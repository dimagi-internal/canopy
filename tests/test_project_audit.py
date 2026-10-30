"""`canopy agent project-audit` — one test per check, plus the never-fail contract.

Fixtures mirror the 2026-10-04 ACE board that motivated it: a registered project with no
folder, Projects/ folders with no project, and a multi-PR task living loose.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from orchestrator import project_audit as pa
from orchestrator.cli import main

NOW = datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc)


def task(ext, *, status="in_progress", project=None, links=0, notes="", updated="2026-09-01T00:00:00Z",
         title=None, next_action="do it", link_urls=None):
    urls = link_urls or [f"https://x/{ext}/{i}" for i in range(links)]
    return {"id": hash(ext) % 1000, "ext_id": ext, "status": status, "project_ext_id": project,
            "title": title or f"Task {ext}", "next_action": next_action, "notes": notes,
            "links": [{"label": "l", "url": u} for u in urls], "updated_at": updated}


def project(ext, name, *, status="active", folder_url="", folder_id=""):
    return {"ext_id": ext, "name": name, "status": status,
            "drive_folder_url": folder_url, "drive_folder_id": folder_id}


def turn(*exts, created="2026-10-04T20:00:00Z"):
    return {"task_ext_ids": list(exts), "created_at": created}


FOLDERS = [{"id": "fold_interviews_01", "name": "Connect Interviews"},
           {"id": "fold_kmc_000000001", "name": "KMC"},
           {"id": "fold_chlorine_0001", "name": "Chlorine Demo"}]


# (a) ------------------------------------------------------------------------------------

def test_loose_task_flagged_on_any_project_signal():
    tasks = [task("T1", links=2), task("T2", notes="a\n\nb"), task("T3"),
             task("T4", links=1, notes="only one")]
    out = pa.check_loose_tasks(tasks, [turn("T3")])
    assert {o["ext_id"]: o["signals"] for o in out} == {
        "T1": ["2 links"], "T2": ["2 note entries"], "T3": ["1 turn record"]}


def test_loose_check_ignores_filed_closed_and_manual_tasks():
    tasks = [task("T1", links=3, project="P1"), task("T2", links=3, status="done"),
             task("T3", links=3, next_action="[MANUAL — Jon handles this] …")]
    assert pa.check_loose_tasks(tasks, []) == []


def test_loose_task_suggests_reregistering_against_its_dormant_folder():
    t = task("T9", links=2, title="KMC topline metrics — weekly audit")
    [hit] = pa.check_loose_tasks([t], [], FOLDERS, projects=[])
    assert hit["matching_folder"] == "KMC" and hit["existing_project"] == ""
    assert hit["matching_folder_url"].endswith("fold_kmc_000000001")


def test_loose_task_points_at_the_active_project_that_already_holds_its_folder():
    t = task("T7", links=2, link_urls=["https://drive.google.com/drive/folders/fold_interviews_01",
                                       "https://x/2"])
    p = project("P2", "Connect Interviews",
                folder_url="https://drive.google.com/drive/folders/fold_interviews_01")
    [hit] = pa.check_loose_tasks([t], [], FOLDERS, projects=[p])
    assert hit["existing_project"] == "P2"


# (b) ------------------------------------------------------------------------------------

def test_active_project_without_folder_is_flagged_and_points_at_a_same_named_folder():
    ps = [project("P1", "UNGA follow-up video"), project("P3", "KMC"),
          project("P4", "Old", status="done"),
          project("P2", "Connect Interviews", folder_url=pa.folder_url("fold_interviews_01"))]
    out = pa.check_projects_without_folder(ps, FOLDERS)
    assert [(o["ext_id"], o["existing_folder_url"]) for o in out] == [
        ("P1", ""), ("P3", pa.folder_url("fold_kmc_000000001"))]


# (c) ------------------------------------------------------------------------------------

def test_dormant_folders_are_counted_but_never_a_finding():
    ps = [project("P2", "Connect Interviews", folder_id="fold_interviews_01")]
    result = pa.audit(slug="ace", tasks=[], projects=ps, turns=[], folders=FOLDERS, now=NOW)
    assert [f["name"] for f in result["dormant_folders"]] == ["KMC", "Chlorine Demo"]
    assert result["closeout"] == "projects: clean"
    assert "expected, not a finding: 2" in pa.render(result)


def test_a_done_project_still_claims_its_folder():
    ps = [project("P5", "KMC", status="done", folder_id="fold_kmc_000000001")]
    assert "KMC" not in [f["name"] for f in pa.dormant_folders(ps, FOLDERS)]


# (d) ------------------------------------------------------------------------------------

def test_name_drift_between_project_and_linked_folder():
    ps = [project("P2", "Interviews", folder_url=pa.folder_url("fold_interviews_01")),
          project("P3", "Elsewhere", folder_url=pa.folder_url("fold_not_under_projects")),
          project("P4", "KMC", folder_id="fold_kmc_000000001")]
    out = pa.check_name_drift(ps, FOLDERS)
    assert [(o["ext_id"], o["folder_name"]) for o in out] == [("P2", "Connect Interviews"),
                                                            ("P3", "")]
    assert "not under Projects/" in out[1]["reason"]
    closed = [project("P5", "Old name", status="archived", folder_url=pa.folder_url("fold_x")),
              project("P6", "Done", status="done", folder_id="fold_kmc_000000001")]
    assert pa.check_name_drift(closed, FOLDERS) == []  # hal 2026-10-30: an archived P1, every turn


def test_folder_id_parsed_from_either_url_shape():
    assert pa.folder_id_from({"drive_folder_url":
                              "https://drive.google.com/drive/folders/1l--VapOsnvwzitpVCHRk2F8H?usp=x"}) \
        == "1l--VapOsnvwzitpVCHRk2F8H"
    assert pa.folder_id_from({"drive_folder_url": "https://drive.google.com/open?id=abcdefghijkl"}) \
        == "abcdefghijkl"


# (e) ------------------------------------------------------------------------------------

def test_recently_touched_task_without_a_turn_record_in_window():
    tasks = [task("T1", updated="2026-10-04T10:00:00Z"),
             task("T2", updated="2026-10-04T10:00:00Z"),
             task("T3", updated="2026-09-01T00:00:00Z"),
             task("T4", updated="2026-10-04T10:00:00Z")]
    turns = [turn("T2"), turn("T4", created="2026-09-20T00:00:00Z")]
    out = pa.check_unrecorded_tasks(tasks, turns, now=NOW, days=2)
    assert [o["ext_id"] for o in out] == ["T1", "T4"]


# audit / closeout / CLI ------------------------------------------------------------------

def test_closeout_counts_findings_and_says_when_drive_was_skipped():
    result = pa.audit(slug="ace", tasks=[task("T1", links=2)],
                      projects=[project("P1", "UNGA follow-up video")], turns=[],
                      folders=None, drive_note="no Drive root", now=NOW)
    assert result["closeout"] == "projects: 1 loose task, 1 project without folder, Drive unchecked"
    assert result["dormant_folders"] is None and result["name_drift"] is None
    assert "skipped (Drive unchecked)" in pa.render(result)


def _hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def fake_http(monkeypatch):
    responses = {
        # Relative to NOW: the CLI's default --recent-days window is measured from the
        # wall clock, so a fixed date ages out of it and fails the test two days later.
        "agents/ace/tasks/": [task("T7", links=4, updated=_hours_ago(1))],
        "agents/ace/projects/": [project("P1", "UNGA follow-up video")],
    }

    def transport(method, url, headers, body):
        path = url.split("/api/")[1]
        if path.startswith("agents/ace/turns/"):
            return 200, json.dumps({"items": [turn("T1")], "total": 1, "offset": 0, "limit": 200})
        return 200, json.dumps(responses[path])

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)


def test_cli_runs_without_drive_and_exits_zero(fake_http, monkeypatch):
    monkeypatch.setattr(pa, "list_project_folders", lambda repo, slug: (None, "no Drive root"))
    r = CliRunner().invoke(main, ["agent", "project-audit", "--slug", "ace"])
    assert r.exit_code == 0, r.output
    assert r.output.strip().splitlines()[-1] == (
        "projects: 1 loose task, 1 project without folder, 1 unrecorded task, Drive unchecked")


def test_cli_json(fake_http, monkeypatch):
    monkeypatch.setattr(pa, "list_project_folders", lambda repo, slug: (FOLDERS, ""))
    r = CliRunner().invoke(main, ["agent", "project-audit", "--slug", "ace", "--json"])
    body = json.loads(r.output)
    assert [t["ext_id"] for t in body["loose_tasks"]] == ["T7"]
    assert len(body["dormant_folders"]) == 3


def test_cli_exits_zero_when_canopy_web_is_down(monkeypatch):
    def boom(method, url, headers, body):
        return 503, "down"
    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", boom)
    r = CliRunner().invoke(main, ["agent", "project-audit", "--slug", "ace"])
    assert r.exit_code == 0
    assert r.output.startswith("projects: audit failed")


# Drive listing ---------------------------------------------------------------------------

def _gog(listings):
    def runner(cmd, **kw):
        parent = cmd[cmd.index("--parent") + 1]
        assert "--max" in cmd  # gog's default page is 20 children
        if parent not in listings:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        files = [{"id": i, "name": n, "mimeType": m} for i, n, m in listings[parent]]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"files": files}), "")
    return runner


def _identity(monkeypatch, root="ROOT"):
    from orchestrator import agent_gdoc
    monkeypatch.setattr(agent_gdoc, "_gdoc_identity_from_opts", lambda *a: agent_gdoc.GdocIdentity(
        slug="ace", account="ace@x", client="canopy", root_folder=root))


def test_drive_listing_reads_folders_under_projects_only(monkeypatch):
    _identity(monkeypatch)
    folders, note = pa.list_project_folders(None, "ace", runner=_gog({
        "ROOT": [("PROJ", "Projects", pa_folder()), ("x", "Process State", pa_folder())],
        "PROJ": [("k", "KMC", pa_folder()), ("d", "stray.pdf", "application/pdf")],
    }))
    assert (folders, note) == ([{"id": "k", "name": "KMC"}], "")


def test_drive_failure_is_reported_not_raised(monkeypatch):
    _identity(monkeypatch)
    folders, note = pa.list_project_folders(None, "ace", runner=_gog({}))
    assert folders is None and "gog drive ls failed" in note


def test_no_drive_root_is_reported(monkeypatch):
    _identity(monkeypatch, root="")
    folders, note = pa.list_project_folders(None, "ace", runner=_gog({}))
    assert folders is None and "GDRIVE_ROOT_FOLDER" in note


def pa_folder():
    from orchestrator.agent_gdoc import FOLDER_MIME
    return FOLDER_MIME


# (f) ------------------------------------------------------------------------------------

GH = "https://github.com/dimagi-internal"


def test_shipped_task_flagged_only_when_every_github_ref_is_done():
    tasks = [
        task("T1", link_urls=[f"{GH}/canopy-web/issues/1255", f"{GH}/canopy-web/pull/1256"]),
        task("T2", link_urls=[f"{GH}/canopy-web/issues/1"], title=f"x {GH}/hal/pull/9"),
        task("T3", links=2),                                    # no GitHub refs at all
        task("T4", link_urls=[f"{GH}/hal/issues/5"]),           # state unknown (no access)
    ]
    states = {"dimagi-internal/canopy-web#1255": "CLOSED",
              "dimagi-internal/canopy-web#1256": "MERGED",
              "dimagi-internal/canopy-web#1": "CLOSED", "dimagi-internal/hal#9": "OPEN"}
    [hit] = pa.check_shipped_tasks(tasks, states)
    assert hit["ext_id"] == "T1"
    assert hit["refs"] == ["dimagi-internal/canopy-web#1255 CLOSED",
                           "dimagi-internal/canopy-web#1256 MERGED"]


def test_shipped_check_ignores_notes_closed_and_manual_tasks():
    tasks = [task("T1", notes=f"history: {GH}/canopy-web/pull/1751 merged"),
             task("T2", status="done", link_urls=[f"{GH}/x/issues/1"]),
             task("T3", next_action="[MANUAL — Jon] …", link_urls=[f"{GH}/x/issues/1"])]
    assert pa.open_task_github_refs(tasks) == []
    assert pa.check_shipped_tasks(tasks, {"dimagi-internal/x#1": "CLOSED"}) == []


def test_parked_and_snoozed_tasks_are_off_queue_for_every_check():
    """hal 2026-10-30: three parked Supply tasks re-flagged every turn until the line was noise."""
    later = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    parked = [task("T1", links=3, next_action="Parked: Jon doesn't want Supply now.",
                   link_urls=[f"{GH}/x/issues/1", f"{GH}/x/issues/2"]),
              task("T2", links=3, next_action="[PARKED until the lab replies] …",
                   updated="2026-10-04T20:00:00Z")]
    snoozed = task("T3", links=3, updated="2026-10-04T20:00:00Z")
    snoozed["snoozed_until"] = later
    lapsed = task("T4", links=3)
    lapsed["snoozed_until"] = "2026-01-01T00:00:00Z"
    tasks = parked + [snoozed, lapsed]
    assert [o["ext_id"] for o in pa.check_loose_tasks(tasks, [])] == ["T4"]
    assert pa.check_unrecorded_tasks(tasks, [], now=NOW) == []
    assert pa.check_shipped_tasks(tasks, {"dimagi-internal/x#1": "CLOSED",
                                          "dimagi-internal/x#2": "CLOSED"}) == []
    assert not pa._is_off_queue(task("T5", next_action="Park the car, then ship"))


def _gh(stdout, rc=0, stderr=""):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    runner.calls = calls
    return runner


def test_fetch_github_states_one_call_and_partial_data_survives_errors():
    out = json.dumps({"data": {"r0": {"issueOrPullRequest": {"state": "MERGED",
                                                             "closedAt": "2026-10-19T23:51:31Z"}},
                               "r1": None},
                      "errors": [{"message": "Could not resolve to a Repository"}]})
    runner = _gh(out, rc=1)
    states, note = pa.fetch_github_states(["o/a#1", "o/private#2"], runner=runner)
    assert (states, note) == ({"o/a#1": {"state": "MERGED",
                                         "closed_at": "2026-10-19T23:51:31Z"}}, "")
    assert len(runner.calls) == 1 and runner.calls[0][:3] == ["gh", "api", "graphql"]
    assert "closedAt" in runner.calls[0][-1]


def test_shipped_check_skips_a_card_touched_after_its_last_ref_closed():
    # 2026-10-22: every one of hal's 9 (f) hits had been updated after its PRs merged —
    # prerequisites shipped, the card's own next step off GitHub. T132 is the real hit:
    # filed 22:19, both PRs merged after it, card never touched again.
    refs = {"dimagi-internal/canopy-web#1712": {"state": "MERGED", "closed_at": "2026-10-19T22:57:15Z"},
            "dimagi-internal/canopy-web#1724": {"state": "MERGED", "closed_at": "2026-10-19T23:51:31Z"}}
    urls = [f"{GH}/canopy-web/pull/1712", f"{GH}/canopy-web/pull/1724"]
    tasks = [task("T132", link_urls=urls, updated="2026-10-19T22:19:58Z"),   # before → flag
             task("T22", link_urls=urls, updated="2026-10-20T10:00:00Z"),    # after → kept open
             task("T5", link_urls=urls, updated="2026-10-19T23:00:00Z")]     # between → flag
    assert [h["ext_id"] for h in pa.check_shipped_tasks(tasks, refs)] == ["T132", "T5"]
    # an unknown close time on ANY ref falls back to flagging — over-report, never hide
    refs["dimagi-internal/canopy-web#1712"] = {"state": "MERGED", "closed_at": None}
    assert [h["ext_id"] for h in pa.check_shipped_tasks(tasks, refs)] == ["T132", "T22", "T5"]


def test_fetch_github_states_never_fails():
    assert pa.fetch_github_states([], runner=_gh("")) == ({}, "")
    states, note = pa.fetch_github_states(["o/a#1"], runner=_gh("", rc=1, stderr="auth"))
    assert states is None and "GitHub unreachable" in note

    def boom(cmd, **kw):
        raise FileNotFoundError("gh")
    states, note = pa.fetch_github_states(["o/a#1"], runner=boom)
    assert states is None and "gh" in note


def test_audit_reports_shipped_and_github_unchecked_in_closeout():
    t = task("T1", link_urls=[f"{GH}/hal/pull/286"])
    r = pa.audit(slug="hal", tasks=[t], projects=[], turns=[], folders=[], now=NOW,
                 github_states={"dimagi-internal/hal#286": "MERGED"})
    assert r["closeout"] == "projects: 1 shipped task still open"
    assert "--task-id T1 --status done" in pa.render(r)
    r = pa.audit(slug="hal", tasks=[t], projects=[], turns=[], folders=[], now=NOW,
                 github_states=None, github_note="GitHub unreachable: auth")
    assert r["shipped_tasks"] is None
    assert r["closeout"] == "projects: clean (GitHub unchecked)"
    assert "(f) Open tasks whose GitHub work is finished: skipped" in pa.render(r)
