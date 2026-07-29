"""One-shot dispatch at a REPO — `canopy project dispatch connect-labs`.

The harness has accepted project turns the whole time; only the CLI could not send
one, so "put a coding session on connect-labs" had to be aimed at the Hal AGENT,
which is a different target that lands the work in a different place.

A project turn differs from an agent turn in three ways, and each of the three fails
SILENTLY if you get it wrong — the reason these are tests and not a paragraph in a
skill file:

  - it needs a WORKSPACE, carried in the request path, because it has no agent to
    derive tenancy from and the server fails a workspace-less project turn closed;
  - the runner must REPORT the project in its capabilities or nothing ever claims
    the turn — it is accepted with a 201 and then sits queued forever. That list is
    reported by the box on every heartbeat (canopy-web #513), so there is no
    client-side way to declare it and no way to skip the check;
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
    blocked_message,
    build_project_turn_payload,
    can_manage,
    classify_runners,
    derive_project_idempotency_key,
    dormant_message,
    project_turns_path,
    resolve_workspace_choice,
)


def _runner(name, *, status="online", ready=True, projects=(), agents=(), rid=None,
            can_manage=True, owner=""):
    return {"id": rid or f"id-{name}", "name": name, "status": status, "ready": ready,
            "ready_note": "" if ready else "emdash CDP unreachable on :9223",
            # canopy-web #509: the list is scoped by tenant, so it can contain runners
            # this caller may see but not mutate. Defaults True, as the schema does.
            "can_manage": can_manage,
            "paired_by_email": owner,
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

def test_a_runner_that_does_not_report_the_project_cannot_serve_it():
    """This is the trap the whole preflight exists for. `claim_next_turn` matches on
    `Q(project__in=runner.project_names())`, so a project the box does not report
    means the turn is accepted and then never claimed by anything."""
    c = classify_runners([_runner("jj-mbp", projects=["canopy-web"])], "connect-labs")
    assert c["blocked"] is True
    assert c["serving"] == []
    assert [r["name"] for r in c["unreported_yours"]] == ["jj-mbp"]


def test_an_UNTENANTED_empty_fleet_is_UNKNOWN_not_blocked():
    """An empty list with nowhere to have looked is no information, not bad
    information.

    canopy-web USED to scope runner listing to `paired_by == caller`, so every caller
    who did not pair a runner saw zero of them — including when a runner declared the
    repo and claimed the turn within seconds. #509 fixed that server-side, but the
    verdict survives for the one read that still has no tenant behind it: an unpinned
    listing by a caller with no workspace memberships."""
    c = classify_runners([], "connect-labs")
    assert c["unknown"] is True
    assert c["blocked"] is False


def test_a_TENANT_SCOPED_empty_fleet_is_BLOCKED_not_unknown():
    """The same empty list means the opposite thing when it came from one tenant.

    `/api/w/<ws>/harness/runners/` answers only after the middleware has gated
    membership of `<ws>`, and #509 scopes the read to the tenant rather than to who
    paired what — so an empty answer is the fleet, not the caller's blind spot. A
    dispatch there would 201 and queue forever, which is exactly what the preflight
    exists to refuse."""
    c = classify_runners([], "connect-labs", tenant_scoped=True)
    assert c["blocked"] is True
    assert c["unknown"] is False


def test_a_visible_fleet_that_cannot_serve_is_still_BLOCKED():
    """The other half of the split: a fleet that WAS inspected and found wanting is
    real information, so the hard refusal stays."""
    c = classify_runners([_runner("jj-mbp", projects=["canopy-web"])], "connect-labs")
    assert c["unknown"] is False
    assert c["blocked"] is True


def test_a_declaring_online_runner_unblocks_the_dispatch():
    c = classify_runners([_runner("jj-mbp", projects=["connect-labs"])], "connect-labs")
    assert c["blocked"] is False
    assert [r["name"] for r in c["serving"]] == ["jj-mbp"]


def test_a_stale_runner_that_REPORTS_the_repo_is_DORMANT_not_a_refusal():
    """The case retiring `--no-preflight` left with no path, so it is asserted rather
    than assumed.

    `claim_next_turn` gates on live_status, so a lapsed runner claims nothing NOW —
    but it is asleep, not gone, and post-#513 the project list it left behind is an
    observation of that box rather than a human's claim about it. A QUEUED turn is
    never expired (canopy-web exempts them so "laptop offline over a weekend must not
    retire Friday's slot"), so the turn is claimed on wake. Refusing would be a false
    negative, and with the escape hatch gone there is nothing to override it with —
    dispatching work for a laptop to pick up when it wakes would just fail."""
    c = classify_runners([_runner("old", status="stale", projects=["connect-labs"])],
                         "connect-labs", tenant_scoped=True)
    assert c["blocked"] is False
    assert [r["name"] for r in c["dormant"]] == ["old"]
    assert c["serving"] == []


def test_a_degraded_runner_that_reports_the_repo_is_a_warning_too():
    """`live_status` serves `degraded` for a runner self-reporting trouble while still
    heartbeating. It cannot claim (claim gates on ONLINE) and it recovers, which is
    the `not ready` case wearing a status instead of a flag — so it warns rather than
    blocks. It previously fell into `offline` and refused."""
    c = classify_runners([_runner("j", status="degraded", projects=["connect-labs"])],
                         "connect-labs", tenant_scoped=True)
    assert c["blocked"] is False
    assert [r["name"] for r in c["degraded"]] == ["j"]


@pytest.mark.parametrize("status", ["disconnected", "retired"])
def test_a_runner_that_will_NOT_come_back_still_blocks(status):
    """The line between dormant and dead, and it has to hold or `dormant` becomes the
    queue-forever bug wearing a warning.

    `disconnected` means the runner has NEVER heartbeated, so any project list on it
    is pre-#513 residue rather than an observation of a box; `retired` is terminal.
    Neither returns, so neither is evidence the turn will ever be claimed."""
    c = classify_runners([_runner("x", status=status, projects=["connect-labs"])],
                         "connect-labs", tenant_scoped=True)
    assert c["blocked"] is True
    assert c["dormant"] == []


def test_a_sleeping_runner_that_does_not_have_the_repo_is_not_dormant():
    """Dormant is "it reported THIS repo and went to sleep", not "it is asleep".
    Without the repo there is no evidence it would ever claim."""
    c = classify_runners([_runner("old", status="stale", projects=["canopy-web"])],
                         "connect-labs", tenant_scoped=True)
    assert c["blocked"] is True
    assert c["dormant"] == []


def test_the_dormant_warning_says_the_turn_waits_rather_than_fails():
    """A queued turn against a sleeping box looks identical to a hang, so the warning
    has to say which one it is."""
    c = classify_runners([_runner("jj-mbp", status="stale", projects=["connect-labs"])],
                         "connect-labs", tenant_scoped=True)
    msg = dormant_message(c)
    assert "jj-mbp" in msg
    assert "QUEUED" in msg and "comes back" in msg


def test_a_declaring_but_not_ready_runner_is_a_warning_not_a_refusal():
    """Not-ready is recoverable — the runner claims once its CDP comes back — so the
    turn is late, not lost. Refusing here would be wrong; saying nothing would leave
    a queued turn looking like a hang."""
    c = classify_runners([_runner("jj-mbp", ready=False, projects=["connect-labs"])],
                         "connect-labs")
    assert c["blocked"] is False
    assert [r["name"] for r in c["degraded"]] == ["jj-mbp"]
    assert c["serving"] == []


# --- can_manage: seeing a runner vs being allowed to act on it ---------------

def test_a_runner_you_cannot_manage_is_bucketed_separately():
    """#509 widened the READ past ownership. A runner you can see and cannot touch
    needs a different remedy — its owner has to open the repo — so it must not be
    pooled with your own machines, whose fix is yours to make."""
    c = classify_runners([_runner("theirs", can_manage=False, owner="ace@dimagi.com")],
                         "connect-labs")
    assert [r["name"] for r in c["unreported_theirs"]] == ["theirs"]
    assert c["unreported_yours"] == []
    assert c["blocked"] is True


def test_blocked_on_a_foreign_fleet_names_the_owner_to_ask():
    """The whole point of surfacing can_manage: tell the caller who to go to, rather
    than describing a machine they cannot touch."""
    c = classify_runners([_runner("theirs", can_manage=False, owner="ace@dimagi.com")],
                         "connect-labs")
    msg = blocked_message(c, "dimagi")
    assert "ace@dimagi.com" in msg
    assert "emdash" not in msg, "do not tell them to fix a box that is not theirs"


def test_blocked_on_your_own_fleet_names_the_machine_and_the_real_fix():
    """Post-#513 the fix is always on a box. The message must name which one and
    both ways to do it — that is the entire replacement for `--declare`."""
    c = classify_runners([_runner("jj-mbp", projects=["canopy-web"])], "connect-labs")
    msg = blocked_message(c, "dimagi")
    assert "jj-mbp" in msg
    assert "emdash" in msg and "RUNNER_PROJECTS" in msg
    assert "--declare" not in msg and "project declare" not in msg


def test_can_manage_defaults_true_for_a_server_that_does_not_send_it():
    """The field arrived with #509; an older canopy-web omits it, and every non-list
    route that returns a runner already proved the caller can act on it."""
    assert can_manage({"name": "old"}) is True


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


def test_cli_dispatch_REFUSES_and_names_the_MACHINE_when_nothing_reports_the_repo(net):
    """The single worst outcome available here is a 201 followed by silence. Fail
    loudly at dispatch time instead — and since canopy-web #513 the remedy is never
    a flag on this command, it is an action on a box. The message has to say which
    box and both ways to do it, because that is all the caller can now do."""
    calls = []
    net(calls, [_runner("jj-mbp", projects=["canopy-web"])])

    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code != 0
    assert not [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]], \
        "must not enqueue a turn nothing can claim"
    assert "jj-mbp" in r.output, "must name the machine to fix"
    assert "emdash" in r.output and "RUNNER_PROJECTS" in r.output
    assert "--declare" not in r.output


def test_cli_dispatch_NEVER_PATCHES_capabilities(net):
    """The done-when of #433: no command PATCHes `projects`. canopy-web replaces the
    list from the box on every heartbeat and 422s a body carrying it, so any PATCH
    from here is both futile and a lie about where the truth lives."""
    calls = []
    net(calls, [_runner("jj-mbp", projects=["connect-labs"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code == 0, r.output
    assert not [c for c in calls if c[0] == "PATCH"]


def test_cli_declare_is_gone():
    """`canopy project declare` PATCHed `capabilities.projects`, which canopy-web
    now refuses outright. Leaving it registered would advertise a capability that
    cannot work."""
    r = CliRunner().invoke(main, ["project", "declare", "connect-labs"])
    assert r.exit_code != 0
    assert "No such command 'declare'" in r.output


def test_cli_declare_FLAG_is_gone(net):
    calls = []
    net(calls, [_runner("jj-mbp", projects=["canopy-web"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs", "--declare",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code != 0
    assert "no such option" in r.output.lower()
    assert not [c for c in calls if c[0] == "PATCH"]


def test_cli_no_preflight_is_gone_so_the_check_cannot_be_routed_around(net):
    """The flag existed because the preflight's answer could not be trusted — the
    fleet was invisible to non-pairers (#509) and the list was hand-typed (#513).
    Both are fixed server-side, so the refusal is a fact, and the one recorded use
    of routing around it (#428) queued a turn forever. No escape hatch."""
    calls = []
    net(calls, [_runner("jj-mbp", projects=["canopy-web"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--no-preflight", "--workspace", "dimagi",
                                  "--prompt", "x"])
    assert r.exit_code != 0
    assert "no such option" in r.output.lower()
    assert not [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]], \
        "an unrecognised flag must never fall through to an unchecked enqueue"


def test_cli_dispatch_REFUSES_when_the_TARGET_WORKSPACE_has_no_runners(net):
    """The empty fleet is now a real answer, because the preflight asks the right
    tenant.

    This case used to warn-and-enqueue: the list was scoped to what the caller had
    paired, so empty carried no information (Hal, who paired none, was told a
    dispatch "would queue forever" and then watched `--no-preflight` enqueue a turn
    claimed within seconds). #509 scopes the read by tenant, and the preflight now
    reads the tenant it enqueues into — so empty means this workspace has no runners,
    and enqueueing would be the queue-forever bug rather than the fix for it."""
    calls = []
    net(calls, [])

    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code != 0
    assert not [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert "no runners" in r.output
    assert "dimagi" in r.output, "must name the tenant it found empty"


def test_cli_dispatch_PREFLIGHTS_THE_TENANT_IT_ENQUEUES_INTO(net):
    """The bug #509 made newly reachable, and the reason this read moved.

    The flat runners route returns the union of every workspace the caller belongs
    to, while the turn goes to exactly ONE. A caller in {dimagi, connect} whose only
    runners live in dimagi would preflight GREEN off those and enqueue into connect,
    where nothing serves the repo — a 201 that queues forever, which is #428 again
    through a different door. So the GET must carry the workspace."""
    calls = []
    net(calls, [_runner("jj-mbp", projects=["connect-labs"])])

    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "connect", "--prompt", "x"])
    assert r.exit_code == 0, r.output
    gets = [c for c in calls if c[0] == "GET" and "/harness/runners/" in c[1]]
    assert gets, "the preflight must read the fleet"
    assert all(g[1].endswith("/api/w/connect/harness/runners/") for g in gets), \
        f"preflight must read the TENANT it enqueues into, got {[g[1] for g in gets]}"


def test_cli_ENQUEUES_with_a_warning_when_the_only_runner_is_ASLEEP(net):
    """The end-to-end proof that retiring `--no-preflight` cost nothing real.

    A runner that reported the repo and went to sleep gets the turn queued for its
    return rather than a refusal — the one legitimate use the flag still had, now
    answered by the preflight itself and, unlike the flag, explained. Without this
    the work would be undispatchable until someone opened a laptop, with no override
    left to reach it."""
    calls = []
    net(calls, [_runner("jj-mbp", status="stale", projects=["connect-labs"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code == 0, r.output
    assert [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert "WARNING" in r.output
    assert "ASLEEP" in r.output and "QUEUED" in r.output


def test_cli_still_REFUSES_when_the_sleeping_runner_lacks_the_repo(net):
    """The other side of the same line: asleep is only a warning when the box
    reported THIS repo. Otherwise nothing here is evidence a turn would ever land,
    and the refusal must survive."""
    calls = []
    net(calls, [_runner("jj-mbp", status="stale", projects=["canopy-web"])])
    r = CliRunner().invoke(main, ["project", "dispatch", "connect-labs",
                                  "--workspace", "dimagi", "--prompt", "x"])
    assert r.exit_code != 0
    assert not [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]


def test_cli_warns_loudly_when_the_only_reporting_runner_is_not_ready(net):
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


def test_cli_runners_says_UNKNOWN_not_blocked_when_the_fleet_is_invisible(net):
    """The diagnostic command has to agree with the preflight, or the user is told
    the dispatch is impossible by one command and watched it work by the other.

    Unscoped, so still UNKNOWN: with no --workspace there is no tenant to attribute
    the emptiness to. The scoped read is the one that concludes."""
    calls = []
    net(calls, [])
    r = CliRunner().invoke(main, ["project", "runners", "connect-labs"])
    assert r.exit_code == 0, r.output
    assert "UNKNOWN" in r.output
    assert "BLOCKED" not in r.output


def test_cli_runners_scoped_to_a_workspace_reads_that_tenant_and_concludes(net):
    """With a tenant named, an empty answer is the fleet — the same conclusion the
    dispatch preflight draws, from the same read."""
    calls = []
    net(calls, [])
    r = CliRunner().invoke(main, ["project", "runners", "connect-labs",
                                  "--workspace", "connect"])
    assert r.exit_code == 0, r.output
    assert [c for c in calls if c[1].endswith("/api/w/connect/harness/runners/")], \
        [c[1] for c in calls]
    assert "BLOCKED" in r.output
    assert "UNKNOWN" not in r.output


def test_cli_runners_shows_which_rows_you_may_act_on(net):
    """Since #509 the list contains runners the caller cannot mutate. Printing them
    with no marker implies every row is actionable, which is how you discover
    ownership from a bare 404 instead of from the listing."""
    calls = []
    net(calls, [_runner("theirs", projects=["canopy-web"],
                        can_manage=False, owner="ace@dimagi.com")])
    r = CliRunner().invoke(main, ["project", "runners"])
    assert r.exit_code == 0, r.output
    assert "ace@dimagi.com" in r.output

    calls2 = []
    net(calls2, [_runner("theirs", can_manage=False, owner="ace@dimagi.com")])
    r2 = CliRunner().invoke(main, ["project", "runners", "--json-output"])
    assert json.loads(r2.output)[0]["can_manage"] is False


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
