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

`--declare` is on borrowed time. canopy-web #513 makes `capabilities["projects"]`
REPORTED by the runner on every heartbeat (a laptop reports emdash's own projects
table, a cloud box reports `RUNNER_PROJECTS`) and 422s any PATCH body carrying
`projects` — at which point declaring a repo from the client is not a thing that
exists, by design: you make a repo routable by making it real on the box, not by
asserting it. #513 has NOT shipped as of 2026-07-28 (the deployed `HeartbeatIn`
carries no `projects` field and the PATCH still accepts one), so `--declare` still
works and stays. What changes here is only that it stops failing obscurely —
`declare_rejected_message` turns the eventual 422 into that sentence rather than a
traceback, so the day #513 deploys the command explains itself. See #433.

That refusal is only sound when the fleet it inspected is the fleet that will be
asked to claim. Two server-side facts decide that, and canopy-web #509 changed one
of them (merged + deployed 2026-07-28):

- **Runner listing is scoped by TENANT, not by `paired_by`.** It used to be the
  latter, so a caller who paired nothing saw zero runners no matter how many were
  live, and refusing on that empty list stated a conclusion drawn from no evidence.
  That is fixed: `_runner_read_q` now serves a workspace's whole fleet to any
  member, and each row carries `can_manage` for the ownership half (seeing a runner
  and being allowed to mutate it are different questions).
- **The read must be taken in the tenant the turn is enqueued into.** This is the
  half #509 made sharper rather than solved. The flat `/api/harness/runners/` route
  returns the union of every workspace the caller belongs to, while the turn goes to
  exactly one (`/api/w/<ws>/harness/turns/`). Before #509 that union was narrowed to
  what you paired and usually collapsed to one tenant; now it genuinely spans them,
  so a two-workspace caller could preflight GREEN off workspace A's runners and
  enqueue into workspace B, where nothing serves the repo — the #428 failure exactly,
  re-reachable through a new door. So the preflight reads
  `/api/w/<ws>/harness/runners/`: the same tenant, asked the same question.

Because that read is tenant-pinned and membership is already gated by the middleware
before it answers, an EMPTY list from it is now a real finding — "this workspace has
no runners" — rather than the absence of evidence it used to be. So the verdicts
split by how the fleet was read: a tenant-pinned read has two (serving → go,
otherwise blocked → refuse), and only an untenanted read keeps the third, `unknown`
(nothing visible and no tenant to attribute it to — warn, enqueue, and point at
`canopy project turns`, the only thing that can settle it).

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


def can_manage(runner: dict) -> bool:
    """Whether THIS caller may mutate `runner` (declare on it, retire it).

    `RunnerOut.can_manage` (canopy-web #509) is the ownership half that the widened,
    tenant-scoped read gave up: the list now shows runners the caller can see but may
    not touch. Defaults True for the older servers that omit the field, and because
    every non-list route returning a RunnerOut resolved it through the act-on gate
    already — reaching such a response at all proves the caller can manage it.
    """
    return bool((runner or {}).get("can_manage", True))


def classify_runners(runners: list[dict], project: str, *,
                     tenant_scoped: bool = False) -> dict:
    """Split the visible fleet by whether it can actually serve `project`.

    Buckets, because each needs a different answer:

    - `serving`   — online, ready, declares it. One of these and the turn lands.
    - `degraded`  — online and declares it, but self-reports not ready (e.g. emdash
                    CDP unreachable). It will claim once it recovers, so this is a
                    warning, not a refusal — the turn is not lost, only late.
    - `declarable` — online, does NOT declare it, and the caller may mutate it.
                    These are the `--declare` targets: the fix for the blocked case.
    - `foreign`   — online and does not declare it, but belongs to someone else
                    (`can_manage` false). Visible since #509 widened the read, and
                    NOT a `--declare` target: PATCHing it 404s. It is the reason the
                    blocked message can say "ask its owner" instead of telling the
                    caller to try something that will fail.

    Everything else (stale/disconnected/retired) is reported as context but is not
    a route. `claim_next_turn` gates on the DERIVED `live_status`, which the API
    already serves in `status`, so a stale runner's declaration means nothing.

    `tenant_scoped` says whether `runners` came from `/api/w/<ws>/harness/runners/`
    (the dispatch preflight) or the flat union route (`canopy project runners` with
    no workspace). It decides only what an EMPTY list means, and that is the whole
    difference between a warning and a refusal: pinned to a tenant whose membership
    the middleware already gated, empty means "this workspace has no runners", which
    is a fact you can act on; unpinned, empty is just the absence of evidence.
    """
    project = (project or "").strip()
    runners = list(runners or [])
    serving, degraded, declarable, foreign, offline = [], [], [], [], []
    for r in runners:
        status = str(r.get("status") or "").strip().lower()
        declares = project in declared_projects(r)
        if status != ONLINE:
            offline.append(r)
        elif declares and r.get("ready", True):
            serving.append(r)
        elif declares:
            degraded.append(r)
        elif can_manage(r):
            declarable.append(r)
        else:
            foreign.append(r)
    return {
        "project": project,
        "serving": serving,
        "degraded": degraded,
        "declarable": declarable,
        "foreign": foreign,
        "offline": offline,
        "tenant_scoped": tenant_scoped,
        # Nothing visible AND no tenant to attribute it to ≠ nothing suitable. Only
        # an unpinned read is the absence of evidence; see `unknown_message`.
        "unknown": not runners and not tenant_scoped,
        # Blocked means: we LOOKED at the fleet that will be asked to claim, and no
        # member of it will ever claim this turn as things stand. An empty
        # tenant-pinned read qualifies — that IS the look, and its answer is none.
        "blocked": (bool(runners) or tenant_scoped) and not serving and not degraded,
    }


def _runner_line(r: dict) -> str:
    projects = declared_projects(r)
    shown = ", ".join(projects[:8]) + ("…" if len(projects) > 8 else "")
    note = str(r.get("ready_note") or r.get("status_note") or "").strip()
    owner = str(r.get("paired_by_email") or "").strip()
    # Only worth printing when the caller cannot act on it — otherwise ownership is
    # noise. When it matters it is the whole answer: who to go ask.
    own = "" if can_manage(r) else f"  (owned by {owner or 'someone else'})"
    return (f"  - {r.get('name', '?')} [{r.get('status', '?')}"
            f"{'' if r.get('ready', True) else ', not ready'}]{own}"
            f"{' — ' + note if note else ''}\n"
            f"      declares: {shown or '(none)'}")


def blocked_message(classified: dict, workspace: str = "") -> str:
    """Why this dispatch cannot land, and exactly how to make it land.

    Written to be read at the moment of failure by someone who does not know the
    capability model exists — because that is the whole point. The alternative is a
    201 and a turn nobody ever claims, which is indistinguishable from success right
    up until you notice, hours later, that nothing happened.
    """
    project = classified["project"]
    where = f" in workspace '{workspace}'" if workspace else ""
    seen = (classified["declarable"] + classified["foreign"] + classified["degraded"]
            + classified["serving"] + classified["offline"])

    # The empty tenant-pinned case. Reachable only since the preflight started
    # reading the tenant it enqueues into: the workspace is real and you are a
    # member (the middleware gated that before answering), it simply has no runners.
    # Naming the workspace is the load-bearing part — the usual cause is dispatching
    # into the wrong one, not a fleet that needs building.
    if not seen:
        return "\n".join([
            f"workspace '{workspace or '?'}' has no runners at all — refusing to enqueue.",
            "",
            "This is the tenant the turn would be created in, and its fleet is empty,",
            "so nothing could ever claim it. Either you meant a different workspace",
            "(--workspace / CANOPY_WEB_WORKSPACE), or this one has no runner paired yet.",
            "",
            f"See what each workspace has:  canopy project runners {project} --workspace <ws>",
        ])

    lines = [
        f"no live runner can serve project '{project}'{where} — refusing to enqueue.",
        "",
        "A runner only claims turns for projects it DECLARES in its capabilities",
        "(harness claim_next_turn matches on capabilities.projects). Enqueueing anyway",
        "would return 201 and then sit QUEUED forever with nothing to claim it.",
        "",
        f"Runners{where}:",
    ]
    lines.extend(_runner_line(r) for r in seen)

    if classified["declarable"]:
        names = [str(r.get("name") or "") for r in classified["declarable"]]
        target = f" --runner {names[0]}" if len(names) > 1 else ""
        lines += [
            "",
            "Fix: have a live runner declare it —",
            f"  canopy project dispatch {project} --declare{target} …",
            f"  (or: canopy project declare {project}{target})",
        ]
    elif classified["foreign"]:
        # Visible-but-not-yours is a distinct dead end from nothing-there, and #509
        # is what made it distinguishable. Telling this caller to run --declare would
        # be sending them at a 404.
        owners = sorted({str(r.get("paired_by_email") or "").strip()
                         for r in classified["foreign"]} - {""})
        who = ", ".join(owners) if owners else "their owner"
        lines += [
            "",
            f"The live runners here belong to someone else ({who}) — you cannot declare",
            "on them. Ask that owner to open the repo on the runner, or pair your own.",
        ]
    else:
        lines += [
            "",
            "No online runner to declare it on. Start the runner (or pair one), then retry.",
        ]
    return "\n".join(lines)


def unknown_message(project: str) -> str:
    """Why an untenanted empty fleet warns instead of refusing.

    This used to be the common case and is now the rare one. canopy-web scoped
    runner listing to `paired_by == caller`, so whoever paired the runner was the
    only identity that could list it and every other user saw an empty fleet — which
    the preflight read as "no live runner can serve this, refusing" even while a
    runner claimed the turn within seconds (observed 2026-07-28 from Hal's identity
    on connect-labs: the preflight refused, then `--no-preflight` enqueued a turn
    that went `running` almost immediately). #509 fixed the cause server-side.

    What survives is the case with no tenant to attribute the emptiness to: an
    unpinned read (`canopy project runners` with no --workspace) by a caller with no
    workspace memberships. There the list is empty because there was nowhere to look,
    not because a fleet was found wanting — so blocking would again assert a fact not
    in evidence. The dispatch path cannot reach this (it always has a workspace, and
    reads through it); its empty answer is BLOCKED, not this.
    """
    return (
        f"cannot see any runners, so whether one serves '{project}' is UNKNOWN — you "
        "have no workspace memberships to read a fleet from, so this is the absence "
        "of evidence rather than an empty fleet. Enqueueing anyway; if nothing claims "
        "it, the turn sits QUEUED (check below)."
    )


DECLARE_RETIRED = (
    "`projects` is reported by the runner, not set by hand — it is replaced on every "
    "heartbeat from what the box actually has.\n\n"
    "To make a repo routable, open it as a project in emdash on that runner (or set "
    "RUNNER_PROJECTS on a cloud runner). Declaring it from here is no longer a thing "
    "that exists.\n\n"
    "`canopy project declare` / `--declare` are being removed; see canopy#433."
)


def declare_rejected_message(detail: str = "") -> str:
    """The 422 that canopy-web #513 will start returning, said in full.

    #513 makes `capabilities["projects"]` reported-by-the-runner and refuses any
    PATCH body carrying it. Until this command is deleted (blocked on #513 actually
    shipping — see the module docstring), the one thing worth guaranteeing is that
    the failure arrives as the explanation above rather than as a traceback with a
    422 in it, which teaches the reader nothing about what to do instead.
    """
    detail = (detail or "").strip()
    server = f"\n\nServer said: {detail}" if detail else ""
    return DECLARE_RETIRED + server


def is_declare_rejection(error_text: str) -> bool:
    """Whether a failed capability PATCH is #513 refusing `projects` specifically.

    Matched against `CanopyError`'s text, which is the only thing the transport
    preserves: ``"{method} {path} -> {status}: {body}"``. Narrow on purpose — a 422
    mentioning `projects` is the reported-capability rule, while any other failure (a
    404 from PATCHing someone else's runner, an auth error) is a different problem
    and must keep its own message.
    """
    text = str(error_text or "").lower()
    return "-> 422" in text and "projects" in text


def with_project_declared(runner: dict, project: str) -> dict:
    """The capabilities dict to PATCH so `runner` also serves `project`.

    Read-modify-write, because `PATCH /api/harness/runners/{id}` REPLACES
    capabilities wholesale — sending `{"projects": [project]}` would silently drop
    the runner's agents, its other projects, and its `sessions: true`.

    Retired by canopy-web #513 the moment it deploys; see `declare_rejected_message`.
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

    Only runners the caller can MANAGE are eligible. Since #509 widened the read past
    ownership, the fleet contains rows whose PATCH would 404 — auto-selecting one of
    those would turn a clear "that runner isn't yours" into an error about a runner
    the caller never chose.
    """
    pool = [r for r in classified["declarable"] + classified["degraded"]
            + classified["serving"] if can_manage(r)]
    if runner_name:
        for r in (classified["declarable"] + classified["foreign"]
                  + classified["degraded"] + classified["serving"]
                  + classified["offline"]):
            if str(r.get("name") or "") == runner_name:
                if not can_manage(r):
                    owner = str(r.get("paired_by_email") or "").strip()
                    raise DispatchError(
                        f"runner '{runner_name}' belongs to "
                        f"{owner or 'someone else'} — you can see it but cannot "
                        "declare on it. Ask them to open the repo on it, or pass a "
                        "runner you paired."
                    )
                return r
        raise DispatchError(f"no visible runner named '{runner_name}'")
    if not pool:
        foreign = classified["foreign"] + [
            r for r in classified["declarable"] + classified["degraded"]
            + classified["serving"] if not can_manage(r)
        ]
        if foreign:
            owners = sorted({str(r.get("paired_by_email") or "").strip()
                             for r in foreign} - {""})
            raise DispatchError(
                "the live runners here belong to "
                + (", ".join(owners) if owners else "someone else")
                + " — you can see them but cannot declare on them. Ask that owner to "
                "open the repo on the runner, or pair your own."
            )
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
