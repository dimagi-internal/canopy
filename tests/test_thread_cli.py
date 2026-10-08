"""`canopy thread …` and the huddle's agreement step, against a fake canopy-web (no network).

The fake implements the /api/threads contract (docs/architecture/agent-threads.md), including
the server-side guard on `thread_message` turns, so the moderator loop is exercised against the
same refusals the real server gives. A message "reply" is scripted: the fake fills a message's
block on the next read, the way canopy-web derives it from the speaker's close-out."""
import datetime as dt
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from click.testing import CliRunner

from orchestrator import thread_cli
from orchestrator.cli import main
from tests.test_huddle_cli import H, FakeWeb, _cell, detail, prop, r1, write_plan

NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)


class FakeThreadWeb(FakeWeb):
    def __init__(self):
        super().__init__()
        self.threads = {}          # id -> record (messages derived from turn posts)
        self.script = {}           # (thread id, n) -> reply block, revealed on the next GET
        self.closes = []
        self.thread_posts = []

    def _out(self, th):
        return {**{k: v for k, v in th.items() if k != "_key"},
                "messages_used": len(th["messages"])}

    def transport(self, method, url, headers, data):
        body = json.loads(data) if data else None
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        if path == "/api/threads/" and method == "POST":
            self.thread_posts.append(body)
            key = (json.dumps(body.get("parent"), sort_keys=True),
                   sorted(p["agent"] for p in body["participants"]))
            for th in self.threads.values():
                if th["status"] == "open" and th["_key"] == key:
                    return 200, json.dumps(self._out(th))
            tid = f"thr-{len(self.threads) + 1:012x}"
            mins = body.get("deadline_minutes", 90)
            th = {"id": tid, "kind": body["kind"], "purpose": body["purpose"],
                  "participants": body["participants"], "moderator": body["moderator"],
                  "parent": body.get("parent") or {}, "context": body.get("context") or "",
                  "max_messages": body.get("max_messages", 4),
                  "deadline_at": (NOW + dt.timedelta(minutes=mins)).isoformat(),
                  "status": "open", "outcome": {}, "created_at": NOW.isoformat(),
                  "closed_at": None, "messages": [], "_key": key}
            self.threads[tid] = th
            return 201, json.dumps(self._out(th))
        if path == "/api/threads/" and method == "GET":
            rows = list(self.threads.values())
            if q.get("parent_key"):
                rows = [t for t in rows
                        if str(t["parent"].get(q["parent_key"])) == q.get("parent_value")]
            return 200, json.dumps([self._out(t) for t in reversed(rows)])
        m = re.match(r"^/api/threads/([^/]+)$", path)
        if m and method == "GET":
            th = self.threads.get(m.group(1))
            if not th:
                return 404, '{"detail": "no thread"}'
            for msg in th["messages"]:
                if msg["block"] is None and (th["id"], msg["n"]) in self.script:
                    msg["block"] = self.script.pop((th["id"], msg["n"]))
                    msg["status"] = "done"
            return 200, json.dumps(self._out(th))
        m = re.match(r"^/api/threads/([^/]+)/close$", path)
        if m and method == "POST":
            th = self.threads[m.group(1)]
            if th["status"] != "open":
                return 409, json.dumps({"detail": f"thread {th['id']} is {th['status']}"})
            self.closes.append(body)
            th.update(status=body["status"], outcome=body.get("outcome") or {},
                      closed_at=NOW.isoformat())
            return 200, json.dumps(self._out(th))
        if path == "/api/harness/turns/" and method == "POST":
            ref = body.get("origin_ref") or {}
            if ref.get("kind") == "thread_message":
                return self._guard(body, ref)
        return super().transport(method, url, headers, data)

    def _guard(self, body, ref):
        """canopy-web's turn-create guard for thread messages (idempotency checked FIRST)."""
        for p in self.turn_posts:
            if p["idempotency_key"] == body["idempotency_key"]:
                return 200, json.dumps({"id": p["_id"], "status": "queued"})
        th = self.threads.get(ref["thread"])
        if th is None or th["status"] != "open":
            return 409, json.dumps({"detail": f"thread {ref['thread']} is closed"})
        if NOW >= dt.datetime.fromisoformat(th["deadline_at"]):
            return 409, json.dumps({"detail": f"thread {th['id']} passed its deadline"})
        if len(th["messages"]) >= th["max_messages"]:
            return 409, json.dumps({"detail": f"thread {th['id']} used its messages"})
        if ref["speaker"] not in [p["agent"] for p in th["participants"]] \
                or ref["speaker"] != body["agent_slug"]:
            return 422, json.dumps({"detail": "speaker is not a participant"})
        if ref["n"] != len(th["messages"]) + 1:
            return 409, json.dumps({"detail": "out of order"})
        tid = f"turn-{len(self.turn_posts) + 1}"
        self.turn_posts.append({**body, "_id": tid})
        th["messages"].append({"n": ref["n"], "speaker": ref["speaker"], "turn_id": tid,
                               "status": "queued", "created_at": NOW.isoformat(),
                               "finished_at": None, "content_hidden": False,
                               "prompt": body.get("prompt"), "block": None,
                               "reply_source": "none", "reply_error": ""})
        return 201, json.dumps({"id": tid, "status": "queued"})


@pytest.fixture()
def web(monkeypatch):
    w = FakeThreadWeb()
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://cw")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", w.transport)
    monkeypatch.setattr(thread_cli, "_now", lambda: NOW)
    monkeypatch.setattr(thread_cli, "_sleep", lambda s: None)
    from orchestrator import huddle_cli
    monkeypatch.setattr(huddle_cli, "_now", lambda: NOW)
    return w


def run(*args):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def blk(tid, n, frm, position, says="ok", proposal=None):
    b = {"thread": tid, "n": n, "from": frm, "says": says, "position": position}
    if proposal is not None:
        b["proposal"] = proposal
    return b


def open_agreement(web, tmp_path, **kw):
    ctx = tmp_path / "ctx.md"
    ctx.write_text("PROPOSAL\nAMEND NOTE")
    args = ["thread", "open", "--kind", "agreement", "--purpose", "Settle echo's change",
            "--participant", "eva:author", "--participant", "echo:asker", "--moderator", "ada",
            "--parent", "huddle=h1", "--parent", "title=Q4 brief", "--context-file", str(ctx)]
    for k, v in kw.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    r = run(*args)
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


# ── open ─────────────────────────────────────────────────────────────────────────
def test_open_posts_the_contract_body_and_is_idempotent(web, tmp_path):
    th = open_agreement(web, tmp_path, max_messages=6, deadline_minutes=30)
    body = web.thread_posts[0]
    assert body == {"kind": "agreement", "purpose": "Settle echo's change",
                    "participants": [{"agent": "eva", "role": "author"},
                                     {"agent": "echo", "role": "asker"}],
                    "moderator": "ada", "parent": {"huddle": "h1", "title": "Q4 brief"},
                    "context": "PROPOSAL\nAMEND NOTE", "max_messages": 6, "deadline_minutes": 30}
    again = open_agreement(web, tmp_path)
    assert again["id"] == th["id"] and len(web.threads) == 1


def test_open_refuses_a_bad_participant_list(web):
    r = run("thread", "open", "--kind", "agreement", "--purpose", "p", "--moderator", "ada",
            "--participant", "eva:author", "--participant", "eva:asker")
    assert r.exit_code == 2 and "distinct" in r.output


# ── say ──────────────────────────────────────────────────────────────────────────
def test_say_dispatches_a_tagged_isolated_stamped_turn(web, tmp_path):
    th = open_agreement(web, tmp_path)
    out = tmp_path / "p.md"
    r = run("thread", "say", "--thread", th["id"], "--to", "eva", "--prompt-out", str(out))
    assert r.exit_code == 0, r.output
    [post] = web.turn_posts
    assert post["agent_slug"] == "eva" and "runner_id" not in post
    assert post["idempotency_key"] == f"thread-{th['id']}-n1"
    assert post["turn_mode"] == "auto"
    ref = post["origin_ref"]
    assert ref["kind"] == "thread_message" and ref["thread"] == th["id"]
    assert ref["n"] == 1 and ref["speaker"] == "eva" and ref["thread_key"] == f"thread:{th['id']}"
    assert ref["dispatched_by"] == "ada"
    assert "PROPOSAL\nAMEND NOTE" in post["prompt"]
    assert "canopy:dispatched-by=ada" in post["prompt"]          # the standard footer
    assert out.read_text() in post["prompt"]


def test_say_refuses_a_non_participant_and_an_outstanding_message(web, tmp_path):
    th = open_agreement(web, tmp_path)
    r = run("thread", "say", "--thread", th["id"], "--to", "hal")
    assert r.exit_code == 2 and "not a participant" in r.output
    assert run("thread", "say", "--thread", th["id"], "--to", "eva").exit_code == 0
    r = run("thread", "say", "--thread", th["id"], "--to", "echo")
    assert r.exit_code == 2 and "still out" in r.output


def test_say_mode_fallback(web, tmp_path):
    th = open_agreement(web, tmp_path)
    web.refuse_mode = True
    orig = web._guard

    def guard(body, ref):
        if "turn_mode" in body:
            return 403, '{"detail": "turn_mode auto needs the agent\'s owner or an admin"}'
        return orig(body, ref)
    web._guard = guard
    r = run("thread", "say", "--thread", th["id"], "--to", "eva")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["mode_fallback"] is True
    assert "turn_mode" not in web.turn_posts[0]


# ── run (the moderator) ──────────────────────────────────────────────────────────
def test_run_moderates_to_agreement_end_to_end(web, tmp_path):
    th = open_agreement(web, tmp_path)
    tid = th["id"]
    rev = {"title": "Q4 brief", "lead": "eva", "why": "public material only"}
    web.script[(tid, 1)] = blk(tid, 1, "eva", "agree", "Taking echo's change", rev)
    web.script[(tid, 2)] = blk(tid, 2, "echo", "agree", "Good")
    r = run("thread", "run", "--thread", tid)
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["status"] == "settled" and out["result"] == "agreed"
    assert web.closes == [{"status": "settled",
                           "outcome": {"result": "agreed", "why": "Good", "proposal": rev}}]
    assert [p["agent_slug"] for p in web.turn_posts] == ["eva", "echo"]
    # message 2's prompt quotes message 1 verbatim
    assert "Taking echo's change" in web.turn_posts[1]["prompt"]
    assert '"why": "public material only"' in web.turn_posts[1]["prompt"]


def test_run_exits_3_while_waiting_and_resumes(web, tmp_path, monkeypatch):
    th = open_agreement(web, tmp_path)
    tid = th["id"]
    r = run("thread", "run", "--thread", tid, "--budget-seconds", "0")
    assert r.exit_code == 3
    out = json.loads(r.stdout)
    assert out["next"] == f"canopy thread run --thread {tid}" and out["status"] == "open"
    assert len(web.turn_posts) == 1
    # a fresh run (stateless) waits on the same message — it never re-sends a new one
    r = run("thread", "run", "--thread", tid, "--budget-seconds", "0")
    assert r.exit_code == 3 and len(web.turn_posts) == 1
    web.script[(tid, 1)] = blk(tid, 1, "eva", "decline", "Out of scope for Q4")
    r = run("thread", "run", "--thread", tid)
    assert r.exit_code == 0
    assert web.threads[tid]["outcome"] == {"result": "not_agreed",
                                           "why": "eva declined: Out of scope for Q4",
                                           "declined_by": "eva"}


def test_run_closes_out_of_budget(web, tmp_path):
    th = open_agreement(web, tmp_path, max_messages=2)
    tid = th["id"]
    web.script[(tid, 1)] = blk(tid, 1, "eva", "counter", proposal={"title": "Q4 brief"})
    web.script[(tid, 2)] = blk(tid, 2, "echo", "counter", proposal={"title": "Q4 brief"})
    r = run("thread", "run", "--thread", tid)
    assert r.exit_code == 0, r.output
    assert web.threads[tid]["status"] == "out_of_budget"
    assert web.threads[tid]["outcome"]["why"] == "ran out of messages"


def test_run_closes_timed_out(web, tmp_path):
    th = open_agreement(web, tmp_path)
    web.threads[th["id"]]["deadline_at"] = (NOW - dt.timedelta(minutes=1)).isoformat()
    r = run("thread", "run", "--thread", th["id"])
    assert r.exit_code == 0
    assert web.threads[th["id"]]["status"] == "timed_out"
    assert web.turn_posts == []


def test_run_on_a_closed_thread_just_reports(web, tmp_path):
    th = open_agreement(web, tmp_path)
    web.threads[th["id"]].update(status="cancelled", outcome={})
    r = run("thread", "run", "--thread", th["id"])
    assert r.exit_code == 0 and json.loads(r.stdout)["status"] == "cancelled"
    assert web.closes == []


def test_run_survives_a_close_race(web, tmp_path, monkeypatch):
    th = open_agreement(web, tmp_path)
    tid = th["id"]
    web.threads[tid]["deadline_at"] = (NOW - dt.timedelta(minutes=1)).isoformat()
    orig = web.transport

    def racing(method, url, headers, data):
        if url.endswith(f"/api/threads/{tid}/close"):
            web.threads[tid].update(status="cancelled")
        return orig(method, url, headers, data)
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", racing)
    r = run("thread", "run", "--thread", tid)
    assert r.exit_code == 0 and json.loads(r.stdout)["status"] == "cancelled"


def test_run_refusal_from_the_guard_is_exit_2(web, tmp_path):
    th = open_agreement(web, tmp_path)
    web._guard = lambda body, ref: (422, '{"detail": "parent turn is a thread_message"}')
    r = run("thread", "run", "--thread", th["id"])
    assert r.exit_code == 2 and "refused" in r.output


def test_show_prints_a_readable_transcript(web, tmp_path):
    th = open_agreement(web, tmp_path)
    tid = th["id"]
    web.script[(tid, 1)] = blk(tid, 1, "eva", "agree", "Taking it", {"title": "Q4 brief"})
    web.script[(tid, 2)] = blk(tid, 2, "echo", "agree", "Good")
    assert run("thread", "run", "--thread", tid).exit_code == 0
    text = run("thread", "show", "--thread", tid).output
    assert f"{tid} — agreement · settled · 2/4 messages" in text
    assert "Outcome: agreed — Good" in text
    assert "#### Message 1 — eva (author) · position: agree\nTaking it" in text


# ── the huddle's agreement step ──────────────────────────────────────────────────
def _amended(web):
    r2 = {"huddle": H, "round": 2, "member": "eva",
          "proposals": [prop("Joint Q4 brief", "eva", ["echo"]), prop("Solo thing", "eva")],
          "critique_answers": [{"title": "t", "answer": "a"}]}
    echo_r3 = {"huddle": H, "round": 3, "member": "echo",
               "answers": [{"title": "Joint Q4 brief", "lead": "eva", "answer": "amend",
                            "note": "public material only; due 10/8 as a gdoc"}]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, r1("echo")),
                            _cell("eva", 2, r2), _cell("echo", 3, echo_r3)])


def _agree(web, tmp_path):
    plan = write_plan(tmp_path)
    r = run("huddle", "agree", "--plan", str(plan))
    assert r.exit_code == 0, r.output
    return plan, json.loads(r.stdout)["threads"]


def test_huddle_agree_opens_one_thread_per_open_amend(web, tmp_path):
    _amended(web)
    _plan, rows = _agree(web, tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert (row["title"], row["author"], row["asker"]) == ("Joint Q4 brief", "eva", "echo")
    assert row["run"] == f"canopy thread run --thread {row['thread']}"
    body = web.thread_posts[0]
    assert body["kind"] == "agreement" and body["moderator"] == "ada"
    assert body["participants"] == [{"agent": "eva", "role": "author"},
                                    {"agent": "echo", "role": "asker"}]
    assert body["parent"] == {"huddle": H, "title": "Joint Q4 brief", "lead": "eva"}
    assert '"why": "why Joint Q4 brief"' in body["context"]
    assert '"public material only; due 10/8 as a gdoc"' in body["context"]
    assert "answers" not in body["context"]
    # idempotent: a re-run gets the same open thread back
    _plan, again = _agree(web, tmp_path)
    assert again[0]["thread"] == row["thread"] and len(web.threads) == 1


def test_huddle_agree_with_no_amends_opens_nothing(web, tmp_path):
    r2 = {"huddle": H, "round": 2, "member": "eva", "proposals": [prop("Solo", "eva")]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("eva", 2, r2)])
    _plan, rows = _agree(web, tmp_path)
    assert rows == [] and web.thread_posts == []


def _settle(web, tid, status, outcome):
    web.threads[tid].update(status=status, outcome=outcome)


def _props(web):
    r = run("huddle", "proposals", "--huddle", H)
    assert r.exit_code == 0, r.output
    return {p["title"]: p for p in json.loads(r.stdout)}


def test_open_thread_leaves_the_amend_open(web, tmp_path):
    _amended(web)
    plan, [row] = _agree(web, tmp_path)
    p = _props(web)["Joint Q4 brief"]
    assert p["answers"] == {"echo": "amend"}
    assert p["threads"]["echo"]["result"] == "open" and p["needs_agreement"] == []
    props = tmp_path / "props.json"
    props.write_text(json.dumps([p]))
    r = run("huddle", "file", "--plan", str(plan), "--outcomes", str(props), "--local",
            str(tmp_path / "r"), "--dry-run")
    held = json.loads(r.stdout)["held"]
    assert held[0]["held"] == f"amend still being settled in thread {row['thread']} (echo)"


def test_agreed_thread_accepts_the_amend_and_files_its_proposal(web, tmp_path):
    _amended(web)
    plan, [row] = _agree(web, tmp_path)
    revised = prop("Joint Q4 brief", "eva", ["echo"], why="public material only; gdoc by 10/8")
    _settle(web, row["thread"], "settled", {"result": "agreed", "why": "Good",
                                            "proposal": revised})
    p = _props(web)["Joint Q4 brief"]
    assert p["answers"] == {"echo": "amend→accepted"}
    assert p["why"] == "public material only; gdoc by 10/8" and p["revised"] is True
    assert p["threads"]["echo"]["id"] == row["thread"]
    props = tmp_path / "props.json"
    props.write_text(json.dumps([p]))
    r = run("huddle", "file", "--plan", str(plan), "--outcomes", str(props),
            "--local", str(tmp_path / "r"))
    assert r.exit_code == 0, r.output
    assert [f["title"] for f in json.loads(r.stdout)["filed"]] == ["Joint Q4 brief"]
    page = f"https://cw/w/connect/huddles/{H}"
    [t] = [t for t in web.tasks["eva"] if t.get("source_url") == page]
    assert "public material only; gdoc by 10/8" in t["rationale"]


@pytest.mark.parametrize("status,outcome,why", [
    ("settled", {"result": "not_agreed", "why": "echo declined: the gdoc is a must"},
     "echo declined: the gdoc is a must"),
    ("out_of_budget", {"result": "not_agreed", "why": "ran out of messages"},
     "ran out of messages"),
    ("timed_out", {}, "ran out of time"),
])
def test_closed_without_agreement_holds_with_a_plain_reason(web, tmp_path, status, outcome, why):
    _amended(web)
    plan, [row] = _agree(web, tmp_path)
    _settle(web, row["thread"], status, outcome)
    p = _props(web)["Joint Q4 brief"]
    assert p["answers"] == {"echo": "amend→not agreed"} and p["why"] == "why Joint Q4 brief"
    props = tmp_path / "props.json"
    props.write_text(json.dumps([p]))
    r = run("huddle", "file", "--plan", str(plan), "--outcomes", str(props),
            "--local", str(tmp_path / "r"), "--dry-run")
    out = json.loads(r.stdout)
    assert out["filed"] == []
    assert out["held"][0]["held"] == f"Eva and Echo didn't agree: {why}"


def test_file_reads_a_thread_that_closed_after_proposals_ran(web, tmp_path):
    _amended(web)
    plan, [row] = _agree(web, tmp_path)
    p = _props(web)["Joint Q4 brief"]                     # still open here
    props = tmp_path / "props.json"
    props.write_text(json.dumps([p]))
    _settle(web, row["thread"], "settled", {"result": "agreed", "why": "ok"})
    r = run("huddle", "file", "--plan", str(plan), "--outcomes", str(props),
            "--local", str(tmp_path / "r"), "--dry-run")
    assert [f["title"] for f in json.loads(r.stdout)["filed"]] == ["Joint Q4 brief"]


def test_needs_agreement_lists_amends_without_a_thread(web, tmp_path):
    _amended(web)
    p = _props(web)["Joint Q4 brief"]
    assert p["needs_agreement"] == ["echo"] and not p.get("threads")


def test_huddle_agree_end_to_end_through_thread_run(web, tmp_path):
    """The leader's whole amend path: agree → thread run → proposals → file."""
    _amended(web)
    plan, [row] = _agree(web, tmp_path)
    tid = row["thread"]
    revised = prop("Joint Q4 brief", "eva", ["echo"], why="public only")
    web.script[(tid, 1)] = blk(tid, 1, "eva", "agree", "Folded the change in", revised)
    web.script[(tid, 2)] = blk(tid, 2, "echo", "agree", "That works")
    assert run("thread", "run", "--thread", tid).exit_code == 0
    p = _props(web)["Joint Q4 brief"]
    assert p["answers"] == {"echo": "amend→accepted"} and p["why"] == "public only"
    # the author's prompt quoted the proposal and echo's change request verbatim
    first = web.turn_posts[0]["prompt"]
    assert "public material only; due 10/8 as a gdoc" in first and "You are eva, the author" in first
