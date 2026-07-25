# tests/test_agent_client.py
import json
from orchestrator.agent_client import AgentClient, AgentIdentity


def make_client(recorder):
    def transport(method, url, headers, body):
        recorder.append((method, url, json.loads(body) if body else None))
        return 200, "{}"
    return AgentClient({"slug": "echo", "name": "Echo", "email": "echo@dimagi-ai.com"},
                       base_url="https://x.test", token="t", transport=transport)


def test_identity_from_dict_or_model():
    c = AgentClient(AgentIdentity(slug="a"), base_url="https://x.test", token="t")
    assert c.slug == "a"
    c2 = AgentClient({"slug": "b"}, base_url="https://x.test", token="t")
    assert c2.slug == "b"


def test_register_posts_identity():
    rec = []
    c = make_client(rec)
    c.register()
    method, url, body = rec[0]
    assert method == "POST"
    assert url == "https://x.test/api/agents/"
    assert body["slug"] == "echo"
    assert body["email"] == "echo@dimagi-ai.com"


import json as _json
from orchestrator.agent_client import BoardCommand


def _recorder_client(responses):
    """responses: list of (status, text) returned in order."""
    calls = []
    seq = list(responses)
    def transport(method, url, headers, body):
        calls.append((method, url, _json.loads(body) if body else None))
        return seq.pop(0)
    c = AgentClient({"slug": "echo"}, base_url="https://x.test", token="t", transport=transport)
    return c, calls


def test_post_sync_and_skills_and_workproducts():
    c, calls = _recorder_client([(200, "{}"), (200, "{}"), (200, "{}")])
    c.post_sync(period_start="2026-06-01", period_end="2026-06-07", title="W",
                doc_url="https://doc", self_grades={"work": "C+"})
    c.put_work_products([{"title": "T", "url": "https://wp"}])
    c.put_skills([{"name": "s", "url": "https://s"}])
    assert calls[0][:2] == ("POST", "https://x.test/api/agents/echo/syncs/")
    assert calls[0][2]["self_grades"] == {"work": "C+"}
    assert calls[1][:2] == ("POST", "https://x.test/api/agents/echo/work-products/")
    assert calls[1][2] == {"work_products": [{"title": "T", "url": "https://wp"}]}
    assert calls[2][:2] == ("PUT", "https://x.test/api/agents/echo/skills/")
    assert calls[2][2] == {"skills": [{"name": "s", "url": "https://s"}]}


def test_pending_commands_parses_models():
    raw = _json.dumps([{"id": 5, "kind": "dispatch", "task_title": "Do it",
                        "created_by": "jj@dimagi.com", "payload": {"note": "go"}}])
    c, calls = _recorder_client([(200, raw)])
    cmds = c.pending_commands()
    assert calls[0][:2] == ("GET", "https://x.test/api/agents/echo/commands?status=pending")
    assert isinstance(cmds[0], BoardCommand)
    assert (cmds[0].id, cmds[0].kind, cmds[0].task_title) == (5, "dispatch", "Do it")


def test_apply_command_and_patch_task_drops_none():
    c, calls = _recorder_client([(200, "{}"), (200, "{}")])
    c.apply_command(5, result_note="done")
    c.patch_task(9, rationale="why", plan=None, status="in_progress")
    assert calls[0] == ("POST", "https://x.test/api/agents/echo/commands/5/apply", {"result_note": "done"})
    assert calls[1] == ("PATCH", "https://x.test/api/agents/echo/tasks/9/", {"rationale": "why", "status": "in_progress"})


def test_sync_tasks_wraps_payload():
    c, calls = _recorder_client([(200, "{}")])
    c.sync_tasks([{"ext_id": "T1", "title": "x"}])
    assert calls[0][:2] == ("POST", "https://x.test/api/agents/echo/tasks/sync")
    assert calls[0][2] == {"tasks": [{"ext_id": "T1", "title": "x"}]}


def test_list_tasks_returns_list():
    raw = _json.dumps([{"ext_id": "T1", "title": "a"}, {"ext_id": "T2", "title": "b"}])
    c, calls = _recorder_client([(200, raw)])
    tasks = c.list_tasks()
    assert calls[0][:2] == ("GET", "https://x.test/api/agents/echo/tasks/")
    assert [t["ext_id"] for t in tasks] == ["T1", "T2"]


def test_list_tasks_unwraps_paginated():
    raw = _json.dumps({"results": [{"ext_id": "T1"}]})
    c, _ = _recorder_client([(200, raw)])
    assert c.list_tasks() == [{"ext_id": "T1"}]


def test_list_syncs_reads_past_syncs_with_limit():
    raw = _json.dumps([{"id": 2, "period_end": "2026-07-16", "title": "Sync #2"}])
    c, calls = _recorder_client([(200, raw)])
    syncs = c.list_syncs(limit=1)
    assert calls[0][:2] == ("GET", "https://x.test/api/agents/echo/syncs/?limit=1")
    assert syncs[0]["period_end"] == "2026-07-16"


def test_list_syncs_unwraps_items_envelope():
    """canopy-web's Page envelope is {"items": [...]}, NOT {"results": [...]}.

    Regression: only "results" was unwrapped, so every paginated endpoint silently
    returned [] — `agent syncs` reported no syncs for an agent with three, and
    manager-sync recomputed its window from project start on every run.
    """
    raw = _json.dumps({"items": [{"id": 5, "period_end": "2026-07-23"}],
                       "total": 1, "offset": 0, "limit": 100})
    c, _ = _recorder_client([(200, raw)])
    assert c.list_syncs() == [{"id": 5, "period_end": "2026-07-23"}]


def test_delete_sync_calls_delete_endpoint():
    c, calls = _recorder_client([(204, "")])
    assert c.delete_sync(5) == {}          # 204, empty body
    assert calls[0][:2] == ("DELETE", "https://x.test/api/agents/echo/syncs/5/")


def test_patch_task_forwards_score_and_review():
    """Completion scoring: `set --status done --score --review` reaches the task PATCH."""
    c, calls = _recorder_client([(200, "{}")])
    c.patch_task(13, status="done", score="A-", review="transcript-grounded")
    assert calls[0] == ("PATCH", "https://x.test/api/agents/echo/tasks/13/",
                        {"status": "done", "score": "A-", "review": "transcript-grounded"})


from pathlib import Path
from orchestrator.agent_client import catalog_from_repo


def test_catalog_from_repo_parses_frontmatter():
    root = Path(__file__).parent / "fixtures" / "agent_skills"
    items = catalog_from_repo(root, "https://gh/{name}/SKILL.md")
    assert items == [{
        "name": "demo-skill",
        "description": "A demo skill used to test catalog parsing. It spans two folded lines.",
        "url": "https://gh/demo-skill/SKILL.md",
        "improvement_note": "",
    }]


def test_post_turn_packages_unit_of_work():
    c, calls = _recorder_client([(200, "{}")])
    c.post_turn(cli_session_id="sess-9", title="Drafted the donor one-pager",
                summary="did X", task_ext_ids=["t1", "t2"],
                work_product_urls=["https://doc/1"], session_slug="abc", share_token="tok")
    method, url, body = calls[0]
    assert (method, url) == ("POST", "https://x.test/api/agents/echo/turns/")
    assert body["cli_session_id"] == "sess-9"
    assert body["task_ext_ids"] == ["t1", "t2"]
    assert body["work_product_urls"] == ["https://doc/1"]
    assert body["share_token"] == "tok" and body["session_slug"] == "abc"


def test_post_turn_transcript_optional_defaults_empty():
    c, calls = _recorder_client([(200, "{}")])
    c.post_turn(cli_session_id="sess-1", title="Packaged, no upload", task_ext_ids=["t1"])
    body = calls[0][2]
    assert body["session_slug"] == "" and body["share_token"] == ""
    assert body["task_ext_ids"] == ["t1"] and body["work_product_urls"] == []


def test_record_verdict_posts_to_step_verdict_endpoint():
    rec = []
    c = make_client(rec)
    c.record_verdict("20260601-1200", "render", kind="judge", score=82.0,
                     criteria={"design": 88}, rationale="solid")
    method, url, body = rec[0]
    assert method == "POST"
    assert url == "https://x.test/api/agents/echo/runs/20260601-1200/steps/render/verdict"
    assert body["kind"] == "judge" and body["score"] == 82.0
    assert body["criteria"] == {"design": 88}
    assert body["rationale"] == "solid"
