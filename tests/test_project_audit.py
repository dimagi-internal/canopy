"""`canopy agent project-audit` — one test per check, plus the never-fail contract.

Fixtures mirror the 2026-10-04 ACE board that motivated it: a registered project with no
folder, Projects/ folders with no project, and a multi-PR task living loose.
"""
import json
import subprocess
from datetime import datetime, timezone

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


@pytest.fixture
def fake_http(monkeypatch):
    responses = {
        "agents/ace/tasks/": [task("T7", links=4, updated="2026-10-04T10:00:00Z")],
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
