"""A DDD run lives on its agent's project on canopy-web (scripts.ddd.run_store).

Why: a run's state lived only on the runner that started it — no other machine
could resume it, two machines could mint the same run id, and nothing said
which piece of work a narrative was for. These tests drive runstate through an
in-memory fake of canopy-web's ``/api/agent-runs/`` contract.
"""
from __future__ import annotations

import datetime as dt

import pytest

from scripts.ddd import gates, run_store, runstate


class FakeWeb:
    """The /api/agent-runs/ + agent-project routes, in memory."""

    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.projects: list[dict] = []
        self.down = False

    def __call__(self, method, path, body=None, query=None):
        if self.down:
            raise run_store.RunStoreError("canopy-web unreachable: test")
        query = query or {}
        if path == "/api/agents/":
            return 200, [{"slug": "ace"}, {"slug": "hal"}]
        if path.startswith("/api/agents/") and path.endswith("/projects/") and method == "POST":
            agent = path.split("/")[3]
            ext = f"P{len([p for p in self.projects if p['agent_slug'] == agent]) + 1}"
            self.projects.append({"agent_slug": agent, "ext_id": ext, "name": body["name"],
                                  "outcome": body.get("outcome", ""), "repo_slug": body.get("repo_slug", ""),
                                  "status": "active"})
            return 201, {"ext_id": ext}
        if path == f"{run_store.BASE}projects/":
            return 200, [p for p in self.projects if p["repo_slug"] == query.get("repo_slug")]
        if path == run_store.BASE and method == "GET":
            out = [r for r in self.runs.values() if r["subject"] == query.get("subject", r["subject"])]
            if query.get("active"):
                out = [r for r in out if r["status"] == "running"]
            return 200, [{k: v for k, v in r.items() if k != "state"} for r in reversed(out)]
        if path == run_store.BASE and method == "POST":
            ext = body.get("ext_id")
            if ext and ext in self.runs:
                return 409, {"detail": "exists"}
            if not ext:
                day = dt.date.today().strftime("%Y-%m-%d")
                prefix = f"{body['subject']}-{day}-"
                nums = [int(k[len(prefix):]) for k in self.runs if k.startswith(prefix)]
                ext = f"{prefix}{max([body.get('min_seq', 1) - 1, *nums]) + 1:03d}"
            proj = next((p for p in self.projects if p["agent_slug"] == body["agent"]
                         and p["ext_id"] == body.get("project")), None)
            self.runs[ext] = {
                "ext_id": ext, "agent_slug": body["agent"], "subject": body["subject"],
                "project": {"ext_id": proj["ext_id"], "name": proj["name"]} if proj else None,
                "state": body.get("state") or {}, "state_version": 1 if body.get("state") else 0,
                "holder": body.get("holder", ""), "status": "running", "current_step": "",
            }
            return 201, dict(self.runs[ext])
        ext = path[len(run_store.BASE):].split("/")[0]
        run = self.runs.get(ext)
        if run is None:
            return 404, {"detail": "not found"}
        if method == "GET":
            return 200, dict(run)
        if method == "PUT":
            if not body.get("force") and body.get("base_version") is not None \
                    and body["base_version"] != run["state_version"]:
                return 409, {"detail": f"last written by {run['holder']}"}
            run.update(state=body["state"], status=body.get("status") or run["status"],
                       holder=body.get("holder") or run["holder"])
            run["state_version"] += 1
            return 200, dict(run)
        raise AssertionError((method, path))


@pytest.fixture
def web(monkeypatch, tmp_path):
    repo = tmp_path / "connect-labs"
    (repo / ".git").mkdir(parents=True)
    ddd = repo / ".canopy" / "ddd"
    ddd.mkdir(parents=True)
    monkeypatch.setenv("CANOPY_DDD_STORE", "web")
    monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("CANOPY_AGENT_SLUG", raising=False)
    monkeypatch.setattr(run_store, "_token", lambda: "t")
    monkeypatch.setattr(run_store, "holder", lambda: "alice@box-a")
    monkeypatch.setattr(gates, "is_unattended", lambda: False)
    fake = FakeWeb()
    monkeypatch.setattr(run_store, "_call", fake)
    fake.ddd = ddd
    return fake


def _bind_project(web, agent="hal", name="Sophie RUTF procurement"):
    status, body = web("POST", f"/api/agents/{agent}/projects/", {"name": name, "repo_slug": "connect-labs"})
    return body["ext_id"]


class TestBinding:
    def test_an_unbound_narrative_asks_which_agent_owns_it(self, web):
        _bind_project(web, "ace", "Nutrition demo")
        with pytest.raises(run_store.NeedsBinding) as exc:
            runstate.new_run("supply-sophie", ddd_dir=web.ddd)
        choices = exc.value.choices
        assert choices["status"] == "unbound" and choices["repo"] == "connect-labs"
        assert [p["name"] for p in choices["projects"]] == ["Nutrition demo"]
        assert choices["agents"] == ["ace", "hal"]
        assert web.runs == {}  # nothing minted until a human picks

    def test_explicit_binding_mints_on_that_project_and_later_runs_follow_it(self, web):
        p = _bind_project(web)
        rid = runstate.new_run("supply-sophie", ddd_dir=web.ddd, agent="hal", project=p)
        assert web.runs[rid]["project"]["name"] == "Sophie RUTF procurement"
        rid2 = runstate.new_run("supply-sophie", ddd_dir=web.ddd)  # no agent: follows the binding
        assert web.runs[rid2]["agent_slug"] == "hal" and rid2.endswith("-002")

    def test_unattended_in_an_agents_turn_that_agent_owns_a_new_project(self, web, monkeypatch):
        monkeypatch.setenv("CANOPY_AGENT_SLUG", "ace")
        monkeypatch.setattr(gates, "is_unattended", lambda: True)
        rid = runstate.new_run("nutrition-demo", ddd_dir=web.ddd)
        assert web.runs[rid]["agent_slug"] == "ace"
        assert web.projects[-1]["repo_slug"] == "connect-labs"

    def test_the_server_id_stays_clear_of_ids_minted_on_disk(self, web):
        p = _bind_project(web)
        day = dt.date.today().strftime("%Y-%m-%d")
        (runstate._resolve_runs_dir(web.ddd) / f"supply-{day}-003").mkdir(parents=True)
        rid = runstate.new_run("supply", ddd_dir=web.ddd, agent="hal", project=p)
        assert rid == f"supply-{day}-004"


class TestWriteThrough:
    def _run(self, web):
        return runstate.new_run("s", ddd_dir=web.ddd, agent="hal", project=_bind_project(web))

    def test_save_writes_through_and_tracks_the_version(self, web):
        rid = self._run(web)
        st = runstate.load(rid, ddd_dir=web.ddd)
        st.iteration = 3
        runstate.save(st, ddd_dir=web.ddd)
        assert web.runs[rid]["state"]["iteration"] == 3
        assert runstate.load(rid, ddd_dir=web.ddd, sync=False).store["version"] == web.runs[rid]["state_version"]

    def test_a_second_runner_driving_the_run_is_refused_and_local_is_untouched(self, web):
        rid = self._run(web)
        st = runstate.load(rid, ddd_dir=web.ddd)
        web.runs[rid].update(state_version=web.runs[rid]["state_version"] + 5, holder="bob@box-b")
        st.iteration = 9
        with pytest.raises(run_store.RunConflict, match="bob@box-b"):
            runstate.save(st, ddd_dir=web.ddd)
        assert runstate.load(rid, ddd_dir=web.ddd, sync=False).iteration == 0

    def test_a_lost_stamp_from_this_same_runner_is_forced(self, web):
        rid = self._run(web)
        st = runstate.load(rid, ddd_dir=web.ddd, sync=False)
        web.runs[rid]["state_version"] += 2  # this runner wrote, then lost its stamp
        st.iteration = 1
        runstate.save(st, ddd_dir=web.ddd)
        assert web.runs[rid]["state"]["iteration"] == 1

    def test_canopy_web_down_saves_locally_and_says_pending(self, web):
        rid = self._run(web)
        st = runstate.load(rid, ddd_dir=web.ddd)
        web.down = True
        st.iteration = 2
        runstate.save(st, ddd_dir=web.ddd)
        local = runstate.load(rid, ddd_dir=web.ddd, sync=False)
        assert local.iteration == 2 and local.store["pending"] is True
        web.down = False
        runstate.save(local, ddd_dir=web.ddd)
        assert web.runs[rid]["state"]["iteration"] == 2


class TestResume:
    def test_a_run_never_seen_here_is_hydrated_from_canopy_web(self, web, tmp_path, monkeypatch):
        rid = runstate.new_run("s", ddd_dir=web.ddd, agent="hal", project=_bind_project(web))
        web.runs[rid]["state"]["iteration"] = 4
        monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "other-machine"))
        st = runstate.load(rid, ddd_dir=web.ddd)
        assert st.iteration == 4 and st.store["agent"] == "hal"
        assert (tmp_path / "other-machine" / rid / "run_state.yaml").exists()

    def test_a_newer_web_copy_wins_over_a_stale_local_one(self, web):
        rid = runstate.new_run("s", ddd_dir=web.ddd, agent="hal", project=_bind_project(web))
        web.runs[rid]["state"] = {**web.runs[rid]["state"], "iteration": 7}
        web.runs[rid]["state_version"] += 3
        assert runstate.load(rid, ddd_dir=web.ddd).iteration == 7


class TestLegacy:
    def test_an_in_flight_local_run_is_adopted_into_its_narratives_project(self, web, monkeypatch):
        monkeypatch.setenv("CANOPY_DDD_STORE", "local")
        rid = runstate.new_run("supply", ddd_dir=web.ddd)
        monkeypatch.setenv("CANOPY_DDD_STORE", "web")
        p = _bind_project(web)
        runstate.new_run("supply", ddd_dir=web.ddd, agent="hal", project=p)  # binds the narrative
        st = runstate.load(rid, ddd_dir=web.ddd)
        assert st.store is None
        runstate.save(st, ddd_dir=web.ddd)
        assert web.runs[rid]["project"]["ext_id"] == p

    def test_an_unbound_legacy_run_stays_local_and_says_how_to_bind(self, web, monkeypatch, capsys):
        monkeypatch.setenv("CANOPY_DDD_STORE", "local")
        rid = runstate.new_run("orphan", ddd_dir=web.ddd)
        monkeypatch.setenv("CANOPY_DDD_STORE", "web")
        st = runstate.load(rid, ddd_dir=web.ddd)
        runstate.save(st, ddd_dir=web.ddd)
        assert rid not in web.runs
        assert "run_store adopt" in capsys.readouterr().err


def test_canopy_web_without_run_documents_falls_back_to_a_local_run(web, monkeypatch, capsys):
    p = _bind_project(web)
    real = web.__call__

    def old_server(method, path, body=None, query=None):
        if path.startswith(run_store.BASE):
            return 404, {"detail": "Not Found"}
        return real(method, path, body, query)

    monkeypatch.setattr(run_store, "_call", old_server)
    rid = runstate.new_run("s", ddd_dir=web.ddd, agent="hal", project=p)
    assert runstate.load(rid, ddd_dir=web.ddd, sync=False).store is None
    assert "LOCAL-ONLY" in capsys.readouterr().err


def test_local_mode_under_pytest_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_DDD_STORE", raising=False)
    assert run_store.mode(tmp_path)[0] == "local"


def test_holder_is_the_process_account_not_logname(monkeypatch):
    """emdash sets LOGNAME=root, which made every account on a machine one
    "runner" — and push() forces a same-runner 409."""
    import os
    import pwd

    monkeypatch.setenv("LOGNAME", "root")
    monkeypatch.setenv("USER", "root")
    assert run_store.holder().split("@")[0] == pwd.getpwuid(os.getuid()).pw_name
