"""A huddle outside a check-in run: `file` → proposed projects (`projects/propose/`) + backings,
falling back loudly to suggested board tasks on a canopy-web without the route; plus the brief /
material helpers check-in huddles share and a dispatch canopy-web refuses.

Check-in runs (proposed TASKS in the project, the run finished) are tests/test_checkin.py."""
import json
import re
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from orchestrator import huddle as Hm
from orchestrator import huddle_cli
from orchestrator.cli import main
from tests.test_huddle_cli import H, NOW, FakeWeb, _full_huddle, write_plan

CHECKIN = {"name": "AI Learning Champions",
           "topic": "Help every team adopt one AI workflow\nthat sticks.",
           "lead_email": "neal@dimagi.com", "channel_name": "ai-champions"}


class ProposeWeb(FakeWeb):
    # canopy-web's ProjectProposeIn — a StrictModel, so any other key is a 422.
    PROPOSE_IN = {"name", "outcome", "why", "plan", "partners", "source_url", "owner_email",
                  "new_evidence"}

    def __init__(self):
        super().__init__()
        self.proposed = {}          # (slug, name) -> project
        self.backings = []          # (slug, ref, member, body)
        self.propose_status = None  # force a status code
        self.refuse_dispatch = set()

    def transport(self, method, url, headers, data):
        body = json.loads(data) if data else None
        path = urlparse(url).path
        assert "/api/huddle-groups/" not in path, f"called a removed route: {method} {path}"
        m = re.match(r"^/api/agents/([^/]+)/projects/propose/$", path)
        if m and method == "POST":
            self.calls.append((method, path, {}, body))
            if self.propose_status:
                return self.propose_status, '{"detail": "Not Found"}'
            key = (m.group(1), body["name"])
            rows = self.projects.setdefault(m.group(1), [])
            if set(body) - self.PROPOSE_IN:
                return 422, json.dumps({"detail": f"extra {sorted(set(body) - self.PROPOSE_IN)}"})
            if not body.get("outcome") or not body.get("why"):
                return 422, '{"detail": "a proposal needs a name, an outcome and a why"}'
            proj = self.proposed.get(key)
            created = proj is None
            if created:
                proj = {"ext_id": f"P{len(rows) + 1}", "status": "proposed", "name": body["name"]}
                rows.append(proj)
                self.proposed[key] = proj
            proj.update({k: body[k] for k in ("outcome", "source_url")})
            proj["_body"] = body
            out = {"id": 1, "agent": m.group(1), "workspace": "connect", "ref": proj["ext_id"],
                   "name": proj["name"], "status": "proposed", "outcome": body["outcome"],
                   "url": f"/agents/{m.group(1)}/projects/{proj['ext_id']}",
                   "decision_task": "T9", "decision_task_id": 9}
            return (201 if created else 200), json.dumps({"created": created, "project": out})
        m = re.match(r"^/api/agents/([^/]+)/projects/([^/]+)/backings/agents/([^/]+)/$", path)
        if m and method == "PUT":
            self.backings = [b for b in self.backings if b[:3] != m.groups()]
            if set(body) - {"stance", "line", "huddle_id"} or len(body.get("line", "")) > 280:
                return 422, '{"detail": "bad backing"}'
            self.backings.append((*m.groups(), body))
            return 200, json.dumps({"stance": body["stance"]})
        if path == "/api/harness/turns/" and method == "POST" and body.get("agent_slug", body.get("agent")) in self.refuse_dispatch:
            return 403, json.dumps({"detail": "ada may not run hal's turns"})
        return super().transport(method, url, headers, data)


@pytest.fixture()
def web(monkeypatch):
    w = ProposeWeb()
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


# ── pure ─────────────────────────────────────────────────────────────────────────
def test_checkin_brief_is_one_numbered_priority_the_brief_contract_reads():
    brief = Hm.checkin_brief(CHECKIN)
    assert Hm.brief_items(brief) == {1: brief[3:]}
    assert brief.startswith("1. Help every team adopt one AI workflow that sticks. — hard dates")
    assert "#ai-champions" in brief and "neal@dimagi.com" in brief
    assert Hm.priority_label(1, brief) == "priority 1: Help every team adopt one AI workflow that sticks."


def test_material_is_quoted_and_a_huge_read_keeps_the_newest_part():
    q, cut = Hm.quote_material("a\n```huddle\n{}\n```\n\nb")
    assert not cut and q.splitlines() == ["> a", "> ```huddle", "> {}", "> ```", ">", "> b"]
    big = "\n".join(f"msg {i}" for i in range(5000))
    q, cut = Hm.quote_material(big, limit=1000)
    assert cut and q.endswith("> msg 4999") and "msg 0\n" not in q and len(q) < 1300
    sec = Hm.checkin_section(CHECKIN, big, {"messages": 5000, "run": 41}, limit=1000)
    assert "only the newest ~1k characters" in sec and "--run 41" in sec


def test_backing_for_maps_round3_answers_to_stances():
    assert Hm.backing_for("co-sign") == {"stance": "backs", "line": ""}
    assert Hm.backing_for("amend→accepted", "fold in X") == {"stance": "backs", "line": "fold in X"}
    assert Hm.backing_for("amend", "x" * 400)["line"] == "x" * 279 + "…"
    assert Hm.backing_for("decline", "not mine") == {"stance": "passes", "line": "not mine"}
    assert Hm.backing_for("") is None


# ── dispatch ─────────────────────────────────────────────────────────────────────
def test_dispatch_refused_by_canopy_web_is_not_reached_not_a_crash(tmp_path, web):
    web.refuse_dispatch = {"eva"}
    p = write_plan(tmp_path)
    prompt = tmp_path / "x.md"
    prompt.write_text("hi")
    r = run("huddle", "dispatch", "--plan", str(p), "--member", "eva", "--round", "1",
            "--prompt-file", str(prompt))
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["dispatched"] is False and "ada may not run hal's turns" in out["not_reached"]


# ── file ─────────────────────────────────────────────────────────────────────────
def _file(tmp_path, p):
    props = tmp_path / "props.json"
    run("huddle", "proposals", "--huddle", H, "--out", str(props))
    return run("huddle", "file", "--plan", str(p), "--outcomes", str(props),
               "--local", str(tmp_path / "rec"))


def test_file_proposes_projects_records_backings_and_finishes_no_run(tmp_path, web):
    _full_huddle(web)
    p = write_plan(tmp_path)
    r = _file(tmp_path, p)
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["filed_as"] == "proposed_projects" and "checkin_run" not in out
    body = web.proposed[("eva", "Joint Q4 brief")]["_body"]
    assert body["partners"] == ["echo"] and body["plan"] == ["start Joint Q4 brief", "then more"]
    assert body["outcome"] == "3 meetings" and "group" not in body
    assert body["source_url"] == f"https://cw/w/connect/huddles/{H}"
    assert web.tasks.get("eva", []) == [] and web.tasks.get("echo", []) == []   # no loose tasks
    assert web.backings == [("eva", "P1", "echo", {"stance": "backs", "line": "", "huddle_id": H})]
    assert not [c for c in web.calls if "/finished" in c[1]]
    rec = json.loads((tmp_path / "rec" / f"{H}.json").read_text())
    assert rec["outcomes"][0]["task"] == {"agent": "eva", "project": "P1", "kind": "proposed_project"}
    assert out["filed"][0]["proposed"]["decision_task"] == "T9"
    # Re-run: revised in place, backings replaced.
    r = _file(tmp_path, p)
    assert r.exit_code == 0, r.output
    assert len(web.projects["eva"]) == 1 and len(web.backings) == 1
    assert json.loads(r.stdout)["filed"][0]["proposed"]["created"] is False


def test_file_falls_back_to_tasks_loudly_on_an_older_server(tmp_path, web):
    _full_huddle(web)
    web.propose_status = 404
    r = _file(tmp_path, write_plan(tmp_path))
    assert r.exit_code == 0, r.output
    assert "FALLING BACK" in r.stderr
    assert json.loads(r.stdout)["filed_as"] == "tasks"
    assert len(web.tasks["eva"]) == 1


def test_file_holds_a_proposal_canopy_web_declined_before(tmp_path, web):
    _full_huddle(web)
    web.propose_status = 409
    r = _file(tmp_path, write_plan(tmp_path))
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["filed"] == [] and any("declined before" in h["held"] for h in out["held"])


def test_file_does_not_fall_back_when_the_lead_agent_is_not_visible(tmp_path, web, monkeypatch):
    _full_huddle(web)
    orig = web.transport

    def t(method, url, headers, data):
        if "/projects/propose/" in url:
            return 404, '{"detail": "agent \'eva\' not found"}'
        return orig(method, url, headers, data)
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", t)
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    run("huddle", "proposals", "--huddle", H, "--out", str(props))
    r = CliRunner().invoke(main, ["huddle", "file", "--plan", str(p), "--outcomes", str(props),
                                  "--local", str(tmp_path / "rec")])
    assert r.exit_code == 1 and "cannot propose on eva" in r.output
    assert not web.tasks.get("eva")
