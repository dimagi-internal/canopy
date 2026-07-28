"""One-shot dispatch at a PROJECT (a repo), the sibling of `agent_dispatch`.

`canopy agent dispatch --slug hal` sends the Hal AGENT at work. There was no way to
send a coding session at a REPO — asked to put an agent on connect-labs, the only
reachable command targeted the agent, which is a different thing and lands the work
in a different place. The harness has supported project turns the whole time
(`TurnIn.project`, `enqueue_turn`'s agent_slug-XOR-project 422, and the runner's
`turn["agent_slug"] or turn["project"]` target resolution); the gap was entirely in
this CLI wrapper.

A project turn is NOT an agent turn with a different field. Three things differ, and
each of the three is a silent failure if you get it wrong:

**1. `workspace` is required, and it does not travel in the body.** An agent turn
derives its tenant from the agent. A project turn has no agent, so it carries its own
workspace FK — supplied by POSTing to the TENANT path `/api/w/<ws>/harness/turns/`
rather than the flat one. Post flat and the server falls back to your default
workspace, which for anyone in two or more workspaces is a 422; `enqueue_turn`
refuses a project turn with no workspace outright, because `claim_next_turn` fails a
null-workspace project turn CLOSED and it would sit queued forever.

**2. A runner only claims projects it DECLARES.** `claim_next_turn` matches on
`Q(project__in=runner.project_names())` — the runner's self-declared
`capabilities["projects"]`. Dispatch at a project no live runner declares and the
turn is accepted (201), looks fine, and is never claimed by anything. That is the
worst outcome available here: it reads exactly like "nothing happened". So the
dispatch preflights the runner fleet and refuses, loudly, rather than enqueueing into
a hole — and, because `PATCH /api/harness/runners/{id}` exists precisely to make
capabilities mutable in place, it can also just fix it (`--declare`).

That refusal is only sound when there was a fleet to inspect. Runner listing is
scoped to `paired_by == caller`, so a caller who paired no runner sees zero runners
no matter how many are live — refusing on an empty list states a conclusion drawn
from no evidence, and did so for every user except the pairer. So the preflight has
three verdicts, not two: serving (go), blocked (a fleet was read and none of it can
claim — refuse), and unknown (nothing visible — warn, enqueue, and point at
`canopy project turns`, which is the only thing that can actually settle it).

**3. There is no board.** `canopy agent dispatch` writes a task to the AGENT's board
first, so the agent finds the item already there when it arrives. A repo has no agent
board — canopy-web's `Project` model carries context/actions, not a task list — and
`Turn.project` is a free string that need not match a registered Project at all. So a
project dispatch writes NO board state. Hanging it off some agent's board would be
inventing a record that nothing reads and nobody owns.

What is deliberately shared with the agent path: `summarize_turn`, so a `done` turn is
still reported as LAUNCHED-not-worked. That discipline is about what a harness turn
means, and it means the same thing for both kinds.

Deterministic: builds payloads, reads fleet state, decides whether a dispatch can
land. Judgment about what to dispatch, and verification that the session did the work,
stay with the caller.
"""
from __future__ import annotations

import hashlib

from orchestrator.agent_dispatch import DispatchError

# Only a runner reporting itself ONLINE can claim (claim_next_turn's first guard,
# on the DERIVED live_status the API serves — a stale heartbeat reads as `stale`).
ONLINE = "online"


def project_turns_path(workspace: str) -> str:
    """The tenant-scoped enqueue path for a project turn.

    Explicit rather than routed through `canopy_web.scoped_api_path`: that helper
    only rewrites the workspace-scoped PRODUCT apps (walkthroughs, ddd, …) and
    deliberately leaves `/api/harness/…` flat, which is right for agent turns and
    wrong for these.
    """
    workspace = (workspace or "").strip()
    if not workspace:
        raise DispatchError(
            "a project turn needs a workspace — it is the turn's tenant, and the "
            "harness fails a workspace-less project turn closed (it would sit "
            "queued forever). Pass --workspace, or set CANOPY_WEB_WORKSPACE."
        )
    return f"/api/w/{workspace}/harness/turns/"


def derive_project_idempotency_key(project: str, title: str, day: str) -> str:
    """Stable key for (project, work, day), in its OWN namespace from the agent key.

    The namespace is load-bearing, not tidiness: `Turn.idempotency_key` is globally
    unique, and this fleet has repos and agents that share a name (`hal`, `ace`,
    `ada`, `echo`, `eva` are all both). A shared derivation would make dispatching
    work at the *repo* silently return the *agent's* turn from earlier that day —
    a 200, no new session, and nothing anywhere saying why.

    Day-scoped for the same reason the agent key is: re-dispatching the same title
    tomorrow is a deliberate re-run, and a permanent key would swallow it.
    """
    project = (project or "").strip()
    digest = hashlib.sha256(f"project|{project}|{title}|{day}".encode()).hexdigest()[:12]
    safe = "".join(c if c.isalnum() else "-" for c in project.lower()).strip("-")
    return f"dispatch-project-{safe}-{digest}"


def build_project_turn_payload(project: str, *, prompt: str = "",
                               idempotency_key: str) -> dict:
    """The `POST /api/w/<ws>/harness/turns/` body for a one-shot project dispatch.

    Note what is NOT here: `agent_slug`. The server enforces agent_slug XOR project
    with a 422, so sending an empty-string agent_slug alongside a project is fine
    (falsy), but omitting it entirely is the honest shape.
    """
    project = (project or "").strip()
    if not project:
        raise DispatchError("dispatch needs a target project (the repo name)")
    if not (idempotency_key or "").strip():
        raise DispatchError("dispatch needs an idempotency key")

    payload = {
        "project": project,
        "origin": "api",
        "idempotency_key": idempotency_key,
        # Same reason as the agent path: with no thread_key the runner resolves
        # `<target>:main` and types the prompt into whatever session already holds
        # that key instead of spawning a fresh one. A one-shot wants isolation by
        # definition, so this is unconditional.
        "origin_ref": {"thread_key": idempotency_key},
    }
    # An absent prompt means "just open a session here" — meaningfully different
    # from an empty one, so send nothing rather than "".
    if (prompt or "").strip():
        payload["prompt"] = prompt
    return payload


def declared_projects(runner: dict) -> list[str]:
    """The repos a runner says it can drive. Mirrors `Runner.project_names()`,
    including the empty-string strip (a session turn has project="", so a stray ""
    in capabilities would make the runner look like it serves every project)."""
    caps = (runner or {}).get("capabilities") or {}
    return [p for p in (caps.get("projects") or []) if p]


def classify_runners(runners: list[dict], project: str) -> dict:
    """Split the visible fleet by whether it can actually serve `project`.

    Three buckets, because they need three different answers:

    - `serving`   — online, ready, declares it. One of these and the turn lands.
    - `degraded`  — online and declares it, but self-reports not ready (e.g. emdash
                    CDP unreachable). It will claim once it recovers, so this is a
                    warning, not a refusal — the turn is not lost, only late.
    - `declarable` — online and ready but does NOT declare it. These are the
                    `--declare` targets: the fix for the blocked case.

    Everything else (stale/disconnected/retired) is reported as context but is not
    a route. `claim_next_turn` gates on the DERIVED `live_status`, which the API
    already serves in `status`, so a stale runner's declaration means nothing.

    A fourth state sits outside the buckets: `unknown`, an empty visible fleet. It
    is NOT the same as a fleet that was inspected and found wanting, and conflating
    the two is what made this preflight wrong for most callers — see
    `unknown_message`.
    """
    project = (project or "").strip()
    runners = list(runners or [])
    serving, degraded, declarable, offline = [], [], [], []
    for r in runners:
        status = str(r.get("status") or "").strip().lower()
        declares = project in declared_projects(r)
        if status != ONLINE:
            offline.append(r)
        elif declares and r.get("ready", True):
            serving.append(r)
        elif declares:
            degraded.append(r)
        else:
            declarable.append(r)
    return {
        "project": project,
        "serving": serving,
        "degraded": degraded,
        "declarable": declarable,
        "offline": offline,
        # Nothing visible ≠ nothing suitable. An empty fleet is the absence of
        # evidence, so no conclusion is available from it at all.
        "unknown": not runners,
        # Blocked means: we LOOKED at the live fleet and no member of it will ever
        # claim this turn as things stand. Requires having seen a fleet to look at.
        "blocked": bool(runners) and not serving and not degraded,
    }


def _runner_line(r: dict) -> str:
    projects = declared_projects(r)
    shown = ", ".join(projects[:8]) + ("…" if len(projects) > 8 else "")
    note = str(r.get("ready_note") or r.get("status_note") or "").strip()
    return (f"  - {r.get('name', '?')} [{r.get('status', '?')}"
            f"{'' if r.get('ready', True) else ', not ready'}]"
            f"{' — ' + note if note else ''}\n"
            f"      declares: {shown or '(none)'}")


def blocked_message(classified: dict) -> str:
    """Why this dispatch cannot land, and exactly how to make it land.

    Written to be read at the moment of failure by someone who does not know the
    capability model exists — because that is the whole point. The alternative is a
    201 and a turn nobody ever claims, which is indistinguishable from success right
    up until you notice, hours later, that nothing happened.
    """
    project = classified["project"]
    lines = [
        f"no live runner can serve project '{project}' — refusing to enqueue.",
        "",
        "A runner only claims turns for projects it DECLARES in its capabilities",
        "(harness claim_next_turn matches on capabilities.projects). Enqueueing anyway",
        "would return 201 and then sit QUEUED forever with nothing to claim it.",
        "",
        "Visible runners:",
    ]
    # Non-empty by construction: `blocked` now requires a fleet to have been seen.
    seen = (classified["declarable"] + classified["degraded"]
            + classified["serving"] + classified["offline"])
    lines.extend(_runner_line(r) for r in seen)

    if classified["declarable"]:
        names = [str(r.get("name") or "") for r in classified["declarable"]]
        target = f" --runner {names[0]}" if len(names) > 1 else ""
        lines += [
            "",
            f"Fix: have a live runner declare it —",
            f"  canopy project dispatch {project} --declare{target} …",
            f"  (or: canopy project declare {project}{target})",
        ]
    else:
        lines += [
            "",
            "No online runner to declare it on. Start the runner (or pair one), then retry.",
        ]
    lines += [
        "",
        "If a runner you cannot see (paired by someone else) serves this project,",
        "re-run with --no-preflight to enqueue anyway.",
    ]
    return "\n".join(lines)


def unknown_message(project: str) -> str:
    """Why an invisible fleet warns instead of refusing.

    canopy-web scopes runner listing to the caller: `_runner_visibility_q`
    (`apps/harness/api.py`) requires `paired_by` to be the caller or null. Whoever
    paired the runner is the only identity that can list it, so every other user
    sees an empty fleet — and an empty fleet said "no live runner can serve this,
    refusing" even while a runner claimed the turn within seconds (observed
    2026-07-28 from Hal's identity on connect-labs: the preflight refused, then
    `--no-preflight` enqueued a turn that went `running` almost immediately).

    Blocking on that asserts a fact not in evidence. The honest report is that the
    preflight had nothing to inspect, so the dispatch proceeds and the caller is
    pointed at the one thing that CAN settle it — the turn's own status.
    """
    return (
        f"cannot see any runners, so whether one serves '{project}' is UNKNOWN — "
        "runner listing shows only the runners YOU paired, and you likely paired "
        "none. Enqueueing anyway: a runner paired by someone else may claim this "
        "within seconds. If nothing does, the turn sits QUEUED (check below)."
    )


def with_project_declared(runner: dict, project: str) -> dict:
    """The capabilities dict to PATCH so `runner` also serves `project`.

    Read-modify-write, because `PATCH /api/harness/runners/{id}` REPLACES
    capabilities wholesale — sending `{"projects": [project]}` would silently drop
    the runner's agents, its other projects, and its `sessions: true`.
    """
    project = (project or "").strip()
    if not project:
        raise DispatchError("nothing to declare — pass a project name")
    caps = dict((runner or {}).get("capabilities") or {})
    caps["projects"] = sorted(set(declared_projects(runner)) | {project})
    return caps


def pick_declare_target(classified: dict, runner_name: str = "") -> dict:
    """Which runner `--declare` should write to.

    Unambiguous or explicit only: silently picking one of several runners decides,
    on the caller's behalf, which machine the session opens on.
    """
    pool = classified["declarable"] + classified["degraded"] + classified["serving"]
    if runner_name:
        for r in pool + classified["offline"]:
            if str(r.get("name") or "") == runner_name:
                return r
        raise DispatchError(f"no visible runner named '{runner_name}'")
    if not pool:
        raise DispatchError("no online runner to declare the project on")
    if len(pool) > 1:
        names = ", ".join(sorted(str(r.get("name") or "") for r in pool))
        raise DispatchError(
            f"several live runners could declare this ({names}) — pass --runner NAME "
            "to say which machine should open the session"
        )
    return pool[0]


def resolve_workspace_choice(explicit: str, env_value: str, memberships) -> str:
    """The workspace a project turn belongs to: explicit → env → sole membership.

    Never guesses between several. The flat harness route already fails that case
    with a 422 naming the tenant path, and a wrong guess here is worse than an
    error: it enqueues a real turn into a tenant whose runners will not claim it.

    `memberships` may be a list or a zero-arg callable, so the caller can hand in
    the fetch itself and skip the round-trip entirely when the answer is already
    pinned — a `--workspace` dispatch should not need the network to read a flag.
    """
    if (explicit or "").strip():
        return explicit.strip()
    if (env_value or "").strip():
        return env_value.strip()
    if callable(memberships):
        memberships = memberships()
    slugs = sorted({s for s in (memberships or []) if s})
    if len(slugs) == 1:
        return slugs[0]
    if not slugs:
        raise DispatchError(
            "cannot resolve a workspace for this project turn — you have no "
            "workspace memberships. Pass --workspace explicitly."
        )
    raise DispatchError(
        "you belong to several workspaces (" + ", ".join(slugs) + ") — pass "
        "--workspace to say which tenant this turn belongs to. It is not cosmetic: "
        "only runners whose pairer is a member of that workspace will claim it."
    )
