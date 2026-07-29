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

**2. A runner only claims projects it REPORTS.** `claim_next_turn` matches on
`Q(project__in=runner.project_names())` — `capabilities["projects"]`. Dispatch at a
project no live runner has and the turn is accepted (201), looks fine, and is never
claimed by anything. That is the worst outcome available here: it reads exactly like
"nothing happened". So the dispatch preflights the runner fleet and refuses, loudly,
rather than enqueueing into a hole.

That list is now REPORTED by the runner, not typed by a human. canopy-web #513
(merged + deployed 2026-07-29) replaces `capabilities["projects"]` on every heartbeat
from what the box actually has — emdash's own projects table on a laptop,
`RUNNER_PROJECTS` on a cloud runner — and 422s any PATCH body carrying `projects`.
So there is no client-side way to make a repo routable, by design: you make it real
on the machine, you do not assert it from here. `canopy project declare` and
`--declare` PATCHed exactly that key and are therefore gone (#433), along with
`--no-preflight`, which existed because the preflight's answer could not be trusted.
Both halves of that distrust are now fixed server-side — the fleet is visible (#509)
and the list is true (#513) — so a refusal from this preflight is a fact, and the
correct response to it is to make the repo real, never to route around the check.
Routing around it is what queued a turn forever in #428.

Removing that flag did leave one case with no path, though, and `dormant` is the
answer to it. #513 changed what the project list IS: an online runner's list is now
fresh by construction, so the only staleness left is TEMPORAL — a runner that
reported the repo and is currently asleep. Blocking there is a false negative, since
a QUEUED turn is never expired and the box claims it on wake; but it is also not a
case for an override, because a flag that skips the whole preflight answers it by
equally waving through the case that genuinely queues forever. So the preflight
answers it itself: warn, enqueue, and say the turn is waiting on a machine. What
stays refused is exactly what would never be claimed.

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

# The two statuses a runner comes BACK from, out of canopy-web's five
# (online/stale/disconnected/degraded/retired). `stale` is a heartbeat that lapsed —
# the box is asleep. `degraded` is self-reported trouble while it is still
# heartbeating, so `live_status` keeps serving it. Neither claims right now, and both
# return.
#
# The other two are excluded deliberately, and that exclusion is what keeps `dormant`
# from becoming the queue-forever bug wearing a warning: `disconnected` means the
# runner has NEVER heartbeated (so any project list on it is pre-#513 residue rather
# than an observation of the box), and `retired` is terminal.
STALE, DEGRADED = "stale", "degraded"
RECOVERABLE = frozenset({STALE, DEGRADED})


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
    """The repos a runner REPORTS it can drive. Mirrors `Runner.project_names()`,
    including the empty-string strip (a session turn has project="", so a stray ""
    in capabilities would make the runner look like it serves every project)."""
    caps = (runner or {}).get("capabilities") or {}
    return [p for p in (caps.get("projects") or []) if p]


def can_manage(runner: dict) -> bool:
    """Whether THIS caller may mutate `runner` (retire it, set agents/sessions).

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

    - `serving`   — online, ready, reports it. One of these and the turn lands.
    - `degraded`  — online and reports it, but self-reports not ready (e.g. emdash
                    CDP unreachable). It will claim once it recovers, so this is a
                    warning, not a refusal — the turn is not lost, only late.
    - `unreported_yours`  — online, does not report the project, and is a machine the
                    caller controls (`can_manage`). The fix is on that box: open the
                    repo in emdash there, or add it to `RUNNER_PROJECTS`.
    - `unreported_theirs` — online, does not report it, and belongs to someone else.
                    Visible since #509 widened the read past ownership. Split out
                    because the remedy is a different person's action, so the message
                    must say "ask its owner" rather than describe a box the caller
                    cannot touch.
    - `dormant`   — reports it, but its heartbeat has lapsed: the box is asleep.
                    A warning rather than a refusal, for two reasons that only hold
                    post-#513. The list is REPORTED by the box, so `stale` means "this
                    repo was really there when the machine last spoke" rather than
                    "someone once typed this"; and a QUEUED turn is never expired
                    (canopy-web's release sweep exempts them so "laptop offline over a
                    weekend must not retire Friday's slot"), so the runner claims it on
                    return. Refusing would be a false negative — and since
                    `--no-preflight` was retired there is no override to escape it
                    with, so the work would simply be undispatchable until someone
                    opened a laptop.

    Everything else (disconnected/retired, and anything not live that does not report
    the project) is context, not a route. `claim_next_turn` gates on the DERIVED
    `live_status`, which the API already serves in `status`.

    `tenant_scoped` says whether `runners` came from `/api/w/<ws>/harness/runners/`
    (the dispatch preflight) or the flat union route (`canopy project runners` with
    no workspace). It decides only what an EMPTY list means, and that is the whole
    difference between a warning and a refusal: pinned to a tenant whose membership
    the middleware already gated, empty means "this workspace has no runners", which
    is a fact you can act on; unpinned, empty is just the absence of evidence.
    """
    project = (project or "").strip()
    runners = list(runners or [])
    serving, degraded, dormant, mine, theirs, offline = [], [], [], [], [], []
    for r in runners:
        status = str(r.get("status") or "").strip().lower()
        reports = project in declared_projects(r)
        if reports and status == ONLINE and r.get("ready", True):
            serving.append(r)
        elif reports and status in (ONLINE, DEGRADED):
            degraded.append(r)
        elif reports and status == STALE:
            dormant.append(r)
        elif status != ONLINE:
            offline.append(r)
        elif can_manage(r):
            mine.append(r)
        else:
            theirs.append(r)
    return {
        "project": project,
        "serving": serving,
        "degraded": degraded,
        "dormant": dormant,
        "unreported_yours": mine,
        "unreported_theirs": theirs,
        "offline": offline,
        "tenant_scoped": tenant_scoped,
        # Nothing visible AND no tenant to attribute it to ≠ nothing suitable. Only
        # an unpinned read is the absence of evidence; see `unknown_message`.
        "unknown": not runners and not tenant_scoped,
        # Blocked means: we LOOKED at the fleet that will be asked to claim, and no
        # member of it will ever claim this turn as things stand. An empty
        # tenant-pinned read qualifies — that IS the look, and its answer is none.
        # `dormant` is not blocked for the same reason `degraded` is not: the runner
        # reports the repo, and the turn survives until it can take it.
        "blocked": ((bool(runners) or tenant_scoped)
                    and not serving and not degraded and not dormant),
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
            f"      reports: {shown or '(none)'}")


def blocked_message(classified: dict, workspace: str = "") -> str:
    """Why this dispatch cannot land, and exactly how to make it land.

    Written to be read at the moment of failure by someone who does not know the
    capability model exists — because that is the whole point. The alternative is a
    201 and a turn nobody ever claims, which is indistinguishable from success right
    up until you notice, hours later, that nothing happened.
    """
    project = classified["project"]
    where = f" in workspace '{workspace}'" if workspace else ""
    seen = (classified["unreported_yours"] + classified["unreported_theirs"]
            + classified["degraded"] + classified["dormant"] + classified["serving"]
            + classified["offline"])

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
        "A runner only claims turns for projects it REPORTS in its capabilities",
        "(harness claim_next_turn matches on capabilities.projects, and canopy-web",
        "replaces that list from the box on every heartbeat). Enqueueing anyway would",
        "return 201 and then sit QUEUED forever with nothing to claim it.",
        "",
        f"Runners{where}:",
    ]
    lines.extend(_runner_line(r) for r in seen)

    # The fix is always on a machine, never in this CLI — canopy-web #513 made
    # `projects` runner-reported precisely so that a repo becomes routable by being
    # real on the box. Which machine, and whose, is the only thing that varies.
    if classified["unreported_yours"]:
        names = [str(r.get("name") or "") for r in classified["unreported_yours"]]
        lines += [
            "",
            f"Fix on the runner ({', '.join(names)}):",
            f"  - laptop: open '{project}' as a project in emdash there",
            f"  - cloud:  add '{project}' to RUNNER_PROJECTS and restart the runner",
            "",
            "The list refreshes on the next heartbeat; then re-run this dispatch.",
        ]
    elif classified["unreported_theirs"]:
        # Visible-but-not-yours is a distinct dead end from nothing-there, and #509
        # is what made it distinguishable.
        owners = sorted({str(r.get("paired_by_email") or "").strip()
                         for r in classified["unreported_theirs"]} - {""})
        who = ", ".join(owners) if owners else "their owner"
        lines += [
            "",
            f"The live runners here belong to someone else ({who}). Ask them to open",
            f"'{project}' on one of those machines, or pair your own runner.",
        ]
    else:
        lines += [
            "",
            "No online runner at all. Start one (or pair one), then retry.",
        ]
    return "\n".join(lines)


def dormant_message(classified: dict) -> str:
    """Why a runner that reports the repo but is asleep warns instead of refusing.

    The judgement this encodes, and the reason it is not simply optimism: `stale`
    means the heartbeat lapsed, not that the runner is gone, and canopy-web #513 makes
    the project list it left behind an OBSERVATION of that box rather than a human's
    assertion about it. So "this repo was really there when the machine last spoke" is
    evidence. And the turn it queues is not lost work — the release sweep deliberately
    exempts QUEUED turns ("laptop offline over a weekend must not retire Friday's
    slot"), so the runner claims it on return.

    This is what #433 item 2 was actually asking about. Retiring `--no-preflight` is
    right — an override that skips the whole check answers this case by also waving
    through the case that genuinely queues forever — but the case itself is real, and
    without this bucket it has no path at all: dispatching work for the laptop to pick
    up when it wakes would simply fail until someone opened it.
    """
    names = ", ".join(sorted(str(r.get("name") or "") for r in classified["dormant"]))
    project = classified["project"]
    return (
        f"the only runner(s) reporting '{project}' ({names}) are ASLEEP — their "
        "heartbeat has lapsed. Enqueueing anyway: the turn stays QUEUED and is claimed "
        "when the machine comes back (a queued turn is never expired). If you need it "
        "run now, wake that machine or use one that is online."
    )


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
