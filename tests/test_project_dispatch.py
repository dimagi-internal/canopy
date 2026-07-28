"""One-shot dispatch at a REPO — `canopy project dispatch connect-labs`.

The harness has accepted project turns the whole time; only the CLI could not send
one, so "put a coding session on connect-labs" had to be aimed at the Hal AGENT,
which is a different target that lands the work in a different place.

A project turn differs from an agent turn in three ways, and each of the three fails
SILENTLY if you get it wrong — the reason these are tests and not a paragraph in a
skill file:

  - it needs a WORKSPACE, carried in the request path, because it has no agent to
    derive tenancy from and the server fails a workspace-less project turn closed;
  - the runner must DECLARE the project in its capabilities or nothing ever claims
    the turn — it is accepted with a 201 and then sits queued forever;
  - its idempotency key must not share a namespace with the agent key, because this
    fleet has repos and agents with the same name and the key column is globally
    unique.

Plus the constraint that made this work worth doing carefully at all: the existing
agent dispatch must behave exactly as before.
"""
import json

import pytest
from click.testing import CliRunner

from orchestrator.agent_dispatch import (
    DispatchError,
    build_turn_payload,
    derive_idempotency_key,
)
from orchestrator.cli import main
from orchestrator.project_dispatch import (
    build_project_turn_payload,
    classify_runners,
    derive_project_idempotency_key,
    pick_declare_target,
    project_turns_path,
    resolve_workspace_choice,
    with_project_declared,
)


def _runner(name, *, status="online", ready=True, projects=(), agents=(), rid=None):
    return {"id": rid or f"id-{name}", "name": name, "status": status, "ready": ready,
            "ready_note": "" if ready else "emdash CDP unreachable on :9223",
            "capabilities": {"agents": list(agents), "projects": list(projects),
                             "sessions": True}}


# --- the payload -------------------------------------------------------------

def test_payload_targets_a_project_and_never_an_agent():
    """The server enforces agent_slug XOR project with a 422. Sending both is a
    hard error, so the project payload must not carry an agent_slug at all."""
    p = build_project_turn_payload("connect-labs", prompt="fix the scope model",
                                   idempotency_key="k1")
    assert p["project"] == "connect-labs"
    assert "agent_slug" not in p
    assert p["prompt"] == "fix the scope model"
    assert p["origin"] == "api"


def test_every_project_dispatch_gets_a_thread_key():
    """Same trap as the agent path: with no thread_key the runner resolves
    `<target>:main` and types the prompt into whatever session already holds that
    key rather than spawning a fresh one."""
    p = build_project_turn_payload("connect-labs", prompt="x", idempotency_key="k9")
    assert p["origin_ref"]["thread_key"] == "k9"


def test_empty_prompt_is_omitted_not_sent_blank():
    assert "prompt" not in build_project_turn_payload("x", prompt="", idempotency_key="k")


@pytest.mark.parametrize("name", ["", "  ", None])
def test_a_project_dispatch_needs_a_target(name):
    with pytest.raises(DispatchError):
        build_project_turn_payload(name, prompt="x", idempotency_key="k")


# --- workspace: the tenant gate ---------------------------------------------

def test_the_turn_goes_to_the_TENANT_path_not_the_flat_one():
    """A project turn's workspace travels in the URL, not the body. The flat route
    falls back to the caller's default workspace, which is a 422 for anyone in two
    or more — and `enqueue_turn` refuses a workspace-less project turn outright
    because `claim_next_turn` fails it closed."""
    assert project_turns_path("dimagi") == "/api/w/dimagi/harness/turns/"


@pytest.mark.parametrize("ws", ["", "   ", None])
def test_no_workspace_is_an_error_not_a_flat_fallback(ws):
    with pytest.raises(DispatchError):
        project_turns_path(ws)


def test_workspace_resolves_explicit_then_env_then_sole_membership():
    assert resolve_workspace_choice("connect", "dimagi", ["a", "b"]) == "connect"
    assert resolve_workspace_choice("", "dimagi", ["a", "b"]) == "dimagi"
    assert resolve_workspace_choice("", "", ["only"]) == "only"


def test_memberships_are_not_fetched_when_the_answer_is_already_pinned():
    """`--workspace` should not need the network to read a flag."""
    def boom():
        raise AssertionError("must not fetch memberships")
    assert resolve_workspace_choice("connect", "", boom) == "connect"
    assert resolve_workspace_choice("", "dimagi", boom) == "dimagi"
    assert resolve_workspace_choice("", "", lambda: ["only"]) == "only"


def test_several_workspaces_is_an_error_never_a_guess():
    """Guessing is worse than failing here: it enqueues a real turn into a tenant
    whose runners will not claim it, which reads as nothing happening."""
    with pytest.raises(DispatchError) as e:
        resolve_workspace_choice("", "", ["dimagi", "connect"])
    assert "dimagi" in str(e.value) and "connect" in str(e.value)


# --- idempotency: the namespace collision ------------------------------------

def test_project_and_agent_keys_never_collide_for_the_same_name():
    """`Turn.idempotency_key` is globally unique, and this fleet has a repo AND an
    agent called `hal`. A shared derivation would make dispatching work at the repo
    silently return the agent's earlier turn — a 200, no new session, no explanation."""
    agent_key = derive_idempotency_key("hal", "Fix the cursor", "2026-07-28")
    project_key = derive_project_idempotency_key("hal", "Fix the cursor", "2026-07-28")
    assert agent_key != project_key


def test_same_project_work_derives_the_same_key():
    a = derive_project_idempotency_key("connect-labs", "Fix scope", "2026-07-28")
    b = derive_project_idempotency_key("connect-labs", "Fix scope", "2026-07-28")
    assert a == b


def test_different_project_work_or_day_derives_a_different_key():
    base = derive_project_idempotency_key("connect-labs", "Fix scope", "2026-07-28")
    assert derive_project_idempotency_key("connect-labs", "Other", "2026-07-28") != base
    assert derive_project_idempotency_key("canopy-web", "Fix scope", "2026-07-28") != base
    assert derive_project_idempotency_key("connect-labs", "Fix scope", "2026-07-29") != base


def test_key_is_url_safe_even_for_a_repo_name_with_punctuation():
    k = derive_project_idempotency_key("dimagi/connect_labs.v2", "T", "2026-07-28")
    assert k.replace("-", "").isalnum()


# --- the capability trap -----------------------------------------------------

def test_a_runner_that_does_not_declare_the_project_cannot_serve_it():
    """This is the trap the whole preflight exists for. `claim_next_turn` matches on
    `Q(project__in=runner.project_names())`, so an undeclared project means the turn
    is accepted and then never claimed by anything."""
    c = classify_runners([_runner("jj-mbp", projects=["canopy-web"])], "connect-labs")
    assert c["blocked"] is True
    assert c["serving"] == []
    assert [r["name"] for r in c["declarable"]] == ["jj-mbp"]


def test_a_declaring_online_runner_unblocks_the_dispatch():
    c = classify_runners([_runner("jj-mbp", projects=["connect-labs"])], "connect-labs")
    assert c["blocked"] is False
    assert [r["name"] for r in c["serving"]] == ["jj-mbp"]


def test_a_stale_runner_declaration_does_not_count():
    """`claim_next_turn` gates on the DERIVED live_status, so a runner whose
    heartbeat lapsed will not claim no matter what it declares."""
    c = classify_runners([_runner("old", status="stale", projects=["connect-labs"])],
                         "connect-labs")
    assert c["blocked"] is True


def test_a_declaring_but_not_ready_runner_is_a_warning_not_a_refusal():
    """Not-ready is recoverable — the runner claims once its CDP comes back — so the
    turn is late, not lost. Refusing here would be wrong; saying nothing would leave
    a queued turn looking like a hang."""
    c = classify_runners([_runner("jj-mbp", ready=False, projects=["connect-labs"])],
                         "connect-labs")
    assert c["blocked"] is False
    assert [r["name"] for r in c["degraded"]] == ["jj-mbp"]
    assert c["serving"] == []


def test_declaring_a_project_preserves_the_rest_of_capabilities():
    """PATCH replaces capabilities wholesale — sending just the new project would
    silently drop the runner's agents, its other repos, and sessions:true."""
    r = _runner("jj-mbp", projects=["canopy-web"], agents=["hal", "ada"])
    caps = with_project_declared(r, "connect-labs")
    assert caps["projects"] == ["canopy-web", "connect-labs"]
    assert caps["agents"] == ["hal", "ada"]
    assert caps["sessions"] is True


def test_declaring_is_idempotent():
    r = _runner("jj-mbp", projects=["connect-labs"])
    assert with_project_declared(r, "connect-labs")["projects"] == ["connect-labs"]


def test_declare_refuses_to_pick_between_several_live_runners():
    """Picking silently would decide, on the caller's behalf, which machine the
    session opens on."""
    c = classify_runners([_runner("a"), _runner("b")], "connect-labs")
    with pytest.raises(DispatchError) as e:
        pick_declare_target(c)
    assert "--runner" in str(e.value)
    assert pick_declare_target(c, "b")["name"] == "b"


# --- CLI ---------------------------------------------------------------------

def _transport(calls, runners, *, turn_status="done"):
    def transport(method, url, headers, data):
        body = json.loads(data) if data else None
        calls.append((method, url, body))
        if "/harness/runners/" in url and method == "GET":
            return 200, json.dumps(runners)
        if "/harness/runners/" in url and method == "PATCH":
            for r in runners:                       # mutate so the re-read sees it
                if r["id"] in url:
                    r["capabilities"] = body["capabilities"]
            return 200, json.dumps({"ok": True})
        if "/workspaces/" in url:
            return 200, json.dumps([{"slug": "dimagi"}])
        if "/harness/turns/" in url and method == "POST":
            return 201, json.dumps({"id": "turn-p1", "status": turn_status,
                                    "project": body.get("project"),
                                    "result_note": "created session 'connect-labs-api-x-0728'"})
        return 200, json.dumps([])
    return transport


@pytest.fixture
def net(monkeypatch):
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_workspace", lambda w=None: None)

    def install(calls, runners, **kw):
        monkeypatch.setattr("orchestrator.canopy_web.urllib_transport",
                            _transport(calls, runners, **kw))
    return install


def test_cli_dispatch_enqueues_a_project_turn_on_the_tenant_path(net):
    calls = []
    net(calls, [_runner("jj-mbp", projects=["connect-labs"])])

    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "fix it",
                                  "--json-output"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["turn"]["state"] == "launched"
    assert out["turn"]["verified"] is False

    posts = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert len(posts) == 1
    assert posts[0][1].endswith("/api/w/dimagi/harness/turns/"), \
        "must POST to the tenant path — the flat one has no workspace to gate on"
    assert posts[0][2]["project"] == "connect-labs"
    assert "agent_slug" not in posts[0][2]
    assert posts[0][2]["origin_ref"]["thread_key"]


def test_cli_dispatch_writes_no_board_task(net):
    """A repo has no agent board. Hanging the record off some agent's board would
    invent state nothing reads and nobody owns."""
    calls = []
    net(calls, [_runner("jj-mbp", projects=["connect-labs"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code == 0, r.output
    assert not [c for c in calls if "/tasks" in c[1]]


def test_cli_dispatch_REFUSES_when_no_runner_declares_the_project(net):
    """The single worst outcome available here is a 201 followed by silence. Fail
    loudly at dispatch time instead, and name the fix."""
    calls = []
    net(calls, [_runner("jj-mbp", projects=["canopy-web"])])

    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code != 0
    assert not [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]], \
        "must not enqueue a turn nothing can claim"
    assert "declare" in r.output.lower()
    assert "--declare" in r.output, "the error must name the remedy"


def test_cli_declare_flag_fixes_the_capability_then_dispatches(net):
    calls = []
    net(calls, [_runner("jj-mbp", projects=["canopy-web"])])

    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs", "--declare",
                                  "--workspace", "dimagi", "--prompt", "x",
                                  "--json-output"])
    assert r.exit_code == 0, r.output
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 1
    assert "connect-labs" in patches[0][2]["capabilities"]["projects"]
    assert "canopy-web" in patches[0][2]["capabilities"]["projects"]
    assert json.loads(r.output)["declared_on"] == "jj-mbp"
    assert [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]


def test_cli_no_preflight_enqueues_without_checking(net):
    """The honest escape hatch: a runner paired by someone else is invisible to this
    caller, so its declaration cannot be checked from here."""
    calls = []
    net(calls, [_runner("jj-mbp", projects=["canopy-web"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs", "--no-preflight",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code == 0, r.output
    assert [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert not [c for c in calls if "/harness/runners/" in c[1]]


def test_cli_warns_loudly_when_the_only_declaring_runner_is_not_ready(net):
    calls = []
    net(calls, [_runner("jj-mbp", ready=False, projects=["connect-labs"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code == 0, r.output
    assert "WARNING" in r.output
    assert "queue" in r.output.lower()


def test_cli_human_output_says_launched_and_how_to_check(net):
    calls = []
    net(calls, [_runner("jj-mbp", projects=["connect-labs"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code == 0, r.output
    assert "launched" in r.output.lower()
    assert "complete" not in r.output.lower()
    assert "canopy project turns" in r.output, "must name the verification command"


def test_cli_runners_reports_the_blocked_case(net):
    calls = []
    net(calls, [_runner("jj-mbp", projects=["canopy-web"])])
    r = CliRunner().invoke(main, ["project", "runners", "connect-labs"])
    assert r.exit_code == 0, r.output
    assert "BLOCKED" in r.output


def test_cli_turns_filters_to_the_named_project(monkeypatch):
    """The harness turn list takes an `agent` filter and has no `project` one, so
    asking the server would silently return every turn in the fleet."""
    def transport(method, url, headers, data):
        return 200, json.dumps([
            {"id": "t-lab", "project": "connect-labs", "status": "done",
             "created_at": "2026-07-28T13:00:00Z",
             "result_note": "created session 'connect-labs-api-x'", "prompt": "fix"},
            {"id": "t-web", "project": "canopy-web", "status": "done",
             "created_at": "2026-07-28T12:00:00Z", "result_note": "", "prompt": "other"},
            {"id": "t-agent", "project": "", "agent_slug": "hal", "status": "done",
             "created_at": "2026-07-28T11:00:00Z", "result_note": "", "prompt": "agent"},
        ])
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)

    r = CliRunner().invoke(main, ["project", "turns", "connect-labs"])
    assert r.exit_code == 0, r.output
    assert "t-lab" in r.output
    assert "t-web" not in r.output and "t-agent" not in r.output
    assert "launched" in r.output.lower()


# --- the constraint: agent dispatch must not change --------------------------

def test_agent_dispatch_payload_is_byte_for_byte_what_it_was(net):
    """Adding a project target must not perturb the agent path. This asserts the
    exact payload shape rather than 'it still works', so a stray `project: ""` — the
    obvious way to unify the two builders — fails here: the server enforces
    agent_slug XOR project by truthiness today, and a shared builder is one refactor
    away from sending both."""
    p = build_turn_payload("hal", prompt="do it", idempotency_key="k1", task_ext_id="T7")
    assert p == {
        "agent_slug": "hal",
        "origin": "api",
        "idempotency_key": "k1",
        "origin_ref": {"thread_key": "k1", "task_ext_id": "T7"},
        "prompt": "do it",
    }


def test_agent_dispatch_still_writes_its_board_task_and_uses_the_FLAT_path(net):
    """The agent path's tenancy comes from the agent, so it must keep POSTing flat.
    Routing it through the new tenant path would change which workspace its turns
    land in."""
    calls = []
    net(calls, [_runner("jj-mbp", agents=["hal"], projects=["connect-labs"])])

    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal",
                                  "--title", "Fix the cursor", "--prompt", "do it",
                                  "--json-output"])
    assert r.exit_code == 0, r.output
    posts = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert len(posts) == 1
    assert posts[0][1].endswith("/api/harness/turns/"), "agent turns stay on the flat path"
    assert posts[0][2]["agent_slug"] == "hal"
    assert "project" not in posts[0][2]
    assert [c for c in calls if "/tasks" in c[1]], "agent dispatch still writes its board task"


def test_agent_dispatch_does_not_preflight_runners(net):
    """Agent dispatch never checked runner capabilities and must not start: an agent
    turn that no runner declares is a pre-existing condition this change is not
    scoped to alter, and adding a network round-trip would change its failure modes."""
    calls = []
    net(calls, [_runner("jj-mbp", agents=[], projects=[])])
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal",
                                  "--prompt", "x", "--no-task"])
    assert r.exit_code == 0, r.output
    assert not [c for c in calls if "/harness/runners/" in c[1]]
