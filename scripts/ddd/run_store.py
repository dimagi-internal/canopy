"""Where a DDD run LIVES — a run document on an agent's PROJECT, on canopy-web.

    python -m scripts.ddd.run_store resolve <narrative>     # bound? to which agent/project?
    python -m scripts.ddd.run_store start <narrative> --agent A (--project P | --new-project NAME [--outcome TEXT])
    python -m scripts.ddd.run_store status <run_id>          # web vs local, who holds it
    python -m scripts.ddd.run_store pull <run_id>            # take the web state onto this runner
    python -m scripts.ddd.run_store push <run_id> [--force]
    python -m scripts.ddd.run_store active [--narrative N]   # in-flight runs, any runner

Why
---
Until 0.2.563 a run's state (iteration, findings, progress, decisions) lived
only in ``~/.canopy/ddd/runs/<repo>/<run_id>/run_state.yaml`` on the machine that
started it. Another runner could not resume it, two runners could mint the same
``run_id`` (canopy-web groups a run's decks and reviews by that string, so their
packages merged), and nothing said which piece of work a narrative was FOR.

Now a run is an ``AgentRun`` document (``kind: ddd``) on an AGENT's PROJECT — the
fleet's generic unit of work toward an outcome (``/agents/<agent>`` → Projects).
Ownership is per project, never per repo: connect-labs carries projects of
several agents (ACE's demos, Hal's product work), and the repo is only the
project's ``repo_slug`` tag. The server mints the run id; ``runstate.save``
writes through; ``runstate.load`` takes the web copy when it is newer or the run
has never been on this machine. The local run dir stays the runner's WORKSPACE
(snapshots, judge cells, clips — a cache the next full render rebuilds).

Binding a narrative to a project
--------------------------------
A narrative's project is the project of its newest run (``resolve``). A
narrative with no run yet is UNBOUND, and the choice of agent + project is the
human's: the orchestrator asks (``resolve`` lists every visible agent and the
projects already touching this repo) and starts the run with ``start``.
Unattended with no human to ask, ``$CANOPY_AGENT_SLUG`` (the agent whose turn
this is) owns a new project named after the narrative; with no agent either, the
run is local-only and says so.

Local-only mode
---------------
``CANOPY_DDD_STORE=local``, no canopy-web token, or a DDD dir outside a repo ->
local-only, announced once per process on stderr. Tests are local-only unless
``CANOPY_DDD_STORE=web``.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

KIND = "ddd"
BASE = "/api/agent-runs/"

_warned: set[str] = set()


class RunStoreError(RuntimeError):
    """The store could not do what was asked (network, auth, server)."""


class RunConflict(RunStoreError):
    """Another runner advanced this run since this one last read it."""


class NeedsBinding(RunStoreError):
    """The narrative has no project yet and nobody said which agent owns it."""

    def __init__(self, narrative: str, choices: dict):
        self.narrative = narrative
        self.choices = choices
        super().__init__(
            f"narrative {narrative!r} is not bound to an agent project yet — ask which agent owns "
            f"it (`python -m scripts.ddd.run_store resolve {narrative}`), then `run_store start`."
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[ddd run store] {msg}", file=sys.stderr)


def holder() -> str:
    """Who is writing: ``<user>@<host>`` — a runner is a user on a machine."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return f"{user}@{socket.gethostname().split('.')[0]}"


def agent_hint() -> str:
    """The agent whose turn this is, if the harness says (``$CANOPY_AGENT_SLUG``)."""
    return os.environ.get("CANOPY_AGENT_SLUG", "").strip()


def repo_slug(ddd_dir: Path) -> str | None:
    """The target repo's name (main checkout, never a worktree's) — the tag an
    agent project carries for the repo it touches."""
    from scripts.ddd.runstate import _enclosing_repo, _repo_identity

    root = _enclosing_repo(Path(ddd_dir).resolve())
    return _repo_identity(root) if root is not None else None


def _token() -> str | None:
    try:
        from scripts.ddd.auth import resolve_token

        return resolve_token(None)
    except Exception:
        return None


def mode(ddd_dir: Path) -> tuple[str, str]:
    """``("web", repo_slug)`` or ``("local", why)``."""
    env = os.environ.get("CANOPY_DDD_STORE", "").strip().lower()
    if env == "local":
        return "local", "CANOPY_DDD_STORE=local"
    if env != "web" and os.environ.get("PYTEST_CURRENT_TEST"):
        return "local", "under pytest (set CANOPY_DDD_STORE=web to exercise the store)"
    repo = repo_slug(ddd_dir)
    if not repo:
        return "local", f"{ddd_dir} is not inside a repo"
    if not _token():
        return "local", "no canopy-web token (CANOPY_WEB_PAT or the workbench token)"
    return "web", repo


def enabled(ddd_dir: Path) -> bool:
    m, why = mode(ddd_dir)
    if m == "web":
        return True
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        _warn_once(
            f"local:{ddd_dir}",
            f"LOCAL-ONLY run storage ({why}). This run is invisible to other runners and to "
            "its agent project on canopy-web.",
        )
    return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _call(method: str, path: str, body: dict | None = None, query: dict | None = None) -> tuple[int, Any]:
    """``(status, json)``; HTTP errors come back as their status, network errors raise."""
    from scripts.ddd.auth import resolve_base_url
    from scripts.ddd.review import _json_request

    token = _token()
    if not token:
        raise RunStoreError("no canopy-web token")
    url = f"{resolve_base_url(None)}{path}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
    try:
        return 200, _json_request(method, url, token, body)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RunStoreError(f"canopy-web unreachable: {exc}") from exc


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _ok(status: int, body: Any, what: str) -> Any:
    if status not in (200, 201):
        raise RunStoreError(f"{what}: HTTP {status} {body}")
    return body


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def runs_for(narrative: str) -> list[dict]:
    status, body = _call("GET", BASE, query={"kind": KIND, "subject": narrative})
    return list(_ok(status, body, "list runs") or [])


def resolve(narrative: str, ddd_dir: Path) -> dict:
    """``{"status": "bound", "agent", "project", "from_run"}`` or
    ``{"status": "unbound", "repo", "agent_hint", "projects": [...], "agents": [...]}``."""
    for run in runs_for(narrative):
        proj = run.get("project") or {}
        if proj.get("ext_id"):
            return {
                "status": "bound", "agent": run["agent_slug"], "project": proj["ext_id"],
                "project_name": proj.get("name"), "from_run": run["ext_id"],
            }
    repo = repo_slug(ddd_dir) or ""
    status, projects = _call("GET", f"{BASE}projects/", query={"repo_slug": repo})
    projects = _ok(status, projects, "list agent projects") or []
    status, agents = _call("GET", "/api/agents/")
    agents = agents.get("items", agents) if isinstance(agents, dict) else agents
    slugs = sorted({a.get("slug") for a in (agents or []) if isinstance(a, dict) and a.get("slug")})
    return {
        "status": "unbound", "narrative": narrative, "repo": repo, "agent_hint": agent_hint(),
        "projects": projects, "agents": slugs,
    }


def create_project(agent: str, name: str, *, repo: str, outcome: str = "") -> str:
    """A new project on ``agent`` touching ``repo``; returns its ext_id."""
    status, body = _call(
        "POST", f"/api/agents/{_q(agent)}/projects/",
        {"name": name, "outcome": outcome, "repo_slug": repo},
    )
    return _ok(status, body, f"create project on {agent}")["ext_id"]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def summary(state: dict) -> dict:
    """The small digest canopy-web lists and shows on the package page."""
    prog = state.get("progress_history") or []
    decks = state.get("iteration_decks") or {}
    it = state.get("iteration")
    return {
        "phase": state.get("phase"),
        "iteration": it,
        "objective": state.get("objective"),
        "loop_mode": state.get("loop_mode"),
        "next_action": state.get("auto_iterate_next_action"),
        "score_history": (state.get("score_history") or [])[-10:],
        "progress": prog[-1] if prog else None,
        "open_gaps": state.get("open_gaps"),
        "deck": decks.get(it) or decks.get(str(it)),
    }


def _status(state: dict) -> str:
    s = state.get("terminal_status")
    return s if s and s != "running" else "running"


def mint(
    narrative: str, *, agent: str, project: str | None, min_seq: int = 1,
    state: dict | None = None, ext_id: str | None = None,
) -> dict:
    """Start a run document; ``ext_id`` adopts a run minted on a runner's disk."""
    body = {
        "agent": agent, "project": project, "kind": KIND, "subject": narrative, "label": narrative,
        "min_seq": max(1, int(min_seq)), "holder": holder(), "state": state or {},
    }
    if ext_id:
        body["ext_id"] = ext_id
    status, resp = _call("POST", BASE, body)
    if status == 409 and ext_id:
        return pull(ext_id) or {}
    return _ok(status, resp, f"start run of {narrative!r}")


def pull(run_id: str) -> dict | None:
    status, body = _call("GET", f"{BASE}{_q(run_id)}/")
    if status == 404:
        return None
    return _ok(status, body, f"GET run {run_id}")


def push(state: dict, *, base_version: int | None, force: bool = False) -> int:
    """Write the state; return the new ``state_version``.

    A 409 whose last writer was this same runner is a lost version stamp, not a
    second driver, and is forced; a 409 naming another runner raises
    :class:`RunConflict`.
    """
    run_id = state["run_id"]
    me = holder()
    prog = state.get("progress_history") or []
    body = {
        "state": state, "base_version": base_version, "force": force, "holder": me,
        "status": _status(state), "iteration": int(state.get("iteration") or 0),
        "score": (prog[-1] or {}).get("score") if prog else None, "summary": summary(state),
    }
    path = f"{BASE}{_q(run_id)}/state/"
    status, resp = _call("PUT", path, body)
    if status == 409 and not force:
        remote = pull(run_id) or {}
        if remote.get("holder") in ("", me):
            body["force"] = True
            status, resp = _call("PUT", path, body)
        else:
            raise RunConflict(
                f"run {run_id} was advanced by {remote.get('holder')} (state_version "
                f"{remote.get('state_version')}; this runner last saw {base_version}). Two runners are "
                f"driving one run. Take theirs: `python -m scripts.ddd.run_store pull {run_id}`; or "
                f"overrule: `python -m scripts.ddd.run_store push {run_id} --force`."
            )
    if status == 404:
        raise RunStoreError(f"run {run_id} is not on canopy-web (start it with run_store start)")
    return int(_ok(status, resp, f"PUT run {run_id} state").get("state_version") or 0)


def active(narrative: str | None = None) -> list[dict]:
    status, body = _call("GET", BASE, query={"kind": KIND, "subject": narrative, "active": "true"})
    return list(_ok(status, body, "list active runs") or [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.run_store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("resolve")
    s.add_argument("narrative")
    s = sub.add_parser("start")
    s.add_argument("narrative")
    s.add_argument("--agent", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--project")
    g.add_argument("--new-project")
    s.add_argument("--outcome", default="")
    s = sub.add_parser("adopt")
    s.add_argument("run_id")
    s.add_argument("--agent", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--project")
    g.add_argument("--new-project")
    s.add_argument("--outcome", default="")
    for name in ("status", "pull"):
        s = sub.add_parser(name)
        s.add_argument("run_id")
    s = sub.add_parser("push")
    s.add_argument("run_id")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("active")
    s.add_argument("--narrative", default=None)
    args = ap.parse_args(argv)

    from scripts.ddd import runstate

    ddd_dir = runstate._resolve_ddd_dir()
    m, val = mode(ddd_dir)
    if m != "web":
        print(json.dumps({"store": "local", "why": val}))
        return 0 if args.cmd in ("status", "resolve") else 2

    if args.cmd == "resolve":
        print(json.dumps(resolve(args.narrative, ddd_dir), indent=1))
    elif args.cmd == "start":
        project = args.project or create_project(
            args.agent, args.new_project, repo=val, outcome=args.outcome
        )
        run_id = runstate.new_run(args.narrative, ddd_dir=ddd_dir, agent=args.agent, project=project)
        print(json.dumps({"run_id": run_id, "agent": args.agent, "project": project}))
    elif args.cmd == "adopt":
        state = runstate.load(args.run_id, ddd_dir=ddd_dir, sync=False)
        project = args.project or create_project(
            args.agent, args.new_project, repo=val, outcome=args.outcome
        )
        mint(state.narrative_slug, agent=args.agent, project=project, ext_id=state.run_id)
        state.store = {"agent": args.agent, "project": project, "version": 0,
                       "synced_at": runstate._now_iso(), "pending": False, "error": None}
        runstate.save(state, ddd_dir=ddd_dir)
        print(json.dumps({"run_id": state.run_id, "agent": args.agent, "project": project,
                          "state_version": (state.store or {}).get("version")}))
    elif args.cmd == "active":
        for r in active(args.narrative):
            proj = (r.get("project") or {}).get("name") or "-"
            print(f"{r['ext_id']}  {r['agent_slug']} / {proj}  {r['current_step'] or '-'}  holder {r['holder'] or '-'}")
    elif args.cmd == "status":
        remote = pull(args.run_id)
        local_file = runstate.run_dir_for(args.run_id, ddd_dir) / "run_state.yaml"
        local = runstate.peek_store(local_file)
        print(json.dumps({
            "web": None if remote is None else {
                k: remote.get(k) for k in ("agent_slug", "project", "state_version", "holder", "holder_at", "status")
            },
            "local": local,
        }, indent=1, default=str))
    elif args.cmd == "pull":
        state = runstate.hydrate(args.run_id, ddd_dir=ddd_dir)
        print(f"pulled {args.run_id} (state_version {(state.store or {}).get('version')})")
    elif args.cmd == "push":
        state = runstate.load(args.run_id, ddd_dir=ddd_dir, sync=False)
        runstate.save(state, ddd_dir=ddd_dir, force=args.force)
        print(f"pushed {args.run_id} (state_version {(state.store or {}).get('version')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
