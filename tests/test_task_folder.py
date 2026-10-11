"""A task's Drive folder is a property of the task (T72).

`canopy gdoc publish --task T71` files under Projects/<project>/<T71 title>, finds the
folder by its ext_id prefix so a reworded title cannot spawn a twin, nests a subtask's
folder inside its parent's, and records the folder on the task.
"""
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from orchestrator import agent_gdoc
from orchestrator.agent_gdoc import AgentGdocError, GdocIdentity
from orchestrator.cli import main
from orchestrator.task_folder import (
    ensure_task_folder,
    resolve_task_destination,
    task_folder_name,
)

FOLDER = "application/vnd.google-apps.folder"


class FakeDrive:
    """`gog drive ls|mkdir` over an in-memory {parent: [children]} tree."""

    def __init__(self, tree=None):
        self.tree = {k: list(v) for k, v in (tree or {}).items()}
        self.created = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        parent = cmd[cmd.index("--parent") + 1]
        if cmd[2] == "ls":
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"files": self.tree.get(parent, [])}))
        fid = f"NEW{len(self.created) + 1}"
        self.created.append((parent, cmd[3]))
        self.tree.setdefault(parent, []).append({"id": fid, "name": cmd[3], "mimeType": FOLDER})
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"folder": {"id": fid}}))


class FakeClient:
    def __init__(self, tasks=(), projects=(), fail_patch=False):
        self.tasks = {t["ext_id"]: dict(t) for t in tasks}
        self.projects = {p["ext_id"]: dict(p) for p in projects}
        self.task_patches, self.project_patches = [], []
        self.fail_patch = fail_patch

    def get_task(self, ref):
        return self.tasks[ref]

    def get_project(self, ref):
        return self.projects[ref]

    def patch_task(self, ref, **fields):
        if self.fail_patch:
            raise RuntimeError("403")
        self.task_patches.append((ref, fields))
        self.tasks[ref].update(fields)
        return self.tasks[ref]

    def patch_project(self, ref, **fields):
        self.project_patches.append((ref, fields))
        self.projects[ref].update(fields)
        return self.projects[ref]


def _ident():
    return GdocIdentity(slug="hal", account="hal@dimagi-ai.com", client="canopy", root_folder="ROOT")


def _t(ext_id, title, project="P5", parent=None, fid=""):
    return {"ext_id": ext_id, "title": title, "project_ext_id": project, "parent_ext_id": parent,
            "drive_folder_id": fid, "drive_folder_url": ""}


P5 = {"ext_id": "P5", "name": "Supply", "drive_folder_id": "P5F", "drive_folder_url": ""}


def test_the_folder_is_named_ext_id_then_title_on_one_line():
    assert task_folder_name(_t("T71", "Sophie's  RUTF\ntender")) == "T71 Sophie's RUTF tender"
    assert len(task_folder_name(_t("T1", "x" * 300))) == 120


def test_a_task_with_no_folder_gets_one_in_its_project_folder_and_it_is_recorded():
    drive = FakeDrive({"P5F": []})
    client = FakeClient([_t("T71", "Sophie's RUTF tender")], [P5])

    got = ensure_task_folder(client, _ident(), client.get_task("T71"), runner=drive)

    assert drive.created == [("P5F", "T71 Sophie's RUTF tender")]
    assert client.task_patches == [("T71", {
        "drive_folder_id": "NEW1", "drive_folder_url": "https://drive.google.com/drive/folders/NEW1"})]
    assert got["created"] and got["linked"]


def test_an_existing_folder_is_found_by_its_ext_id_even_after_the_title_changed():
    drive = FakeDrive({"P5F": [{"id": "OLD", "name": "T71 Sophie's RUTF tender", "mimeType": FOLDER},
                               {"id": "OTHER", "name": "T710 something", "mimeType": FOLDER}]})
    client = FakeClient([_t("T71", "RUTF tender — Kenya round")], [P5])

    got = ensure_task_folder(client, _ident(), client.get_task("T71"), runner=drive)

    assert drive.created == []
    assert got["folder_id"] == "OLD"
    assert client.task_patches[0][1]["drive_folder_id"] == "OLD"


def test_a_linked_folder_wins_and_nothing_is_written():
    drive = FakeDrive()
    client = FakeClient([_t("T71", "x", fid="LINKED")], [P5])

    got = ensure_task_folder(client, _ident(), client.get_task("T71"), runner=drive)

    assert got["folder_id"] == "LINKED"
    assert drive.created == [] and client.task_patches == []


def test_a_subtask_folder_nests_inside_its_parent_task_folder():
    drive = FakeDrive({"P5F": [{"id": "T66F", "name": "T66 Project history", "mimeType": FOLDER}]})
    client = FakeClient([_t("T66", "Project history"), _t("T73", "Two-pager", parent="T66")], [P5])

    got = ensure_task_folder(client, _ident(), client.get_task("T73"), runner=drive)

    assert drive.created == [("T66F", "T73 Two-pager")]
    # The parent's folder was found, so it is recorded on the parent too.
    assert ("T66", {"drive_folder_id": "T66F",
                    "drive_folder_url": "https://drive.google.com/drive/folders/T66F"}) in client.task_patches
    assert got["folder_id"] == "NEW1"


def test_the_project_folder_is_made_and_linked_first_when_the_project_has_none():
    drive = FakeDrive({"ROOT": [{"id": "PROJ", "name": "Projects", "mimeType": FOLDER}]})
    client = FakeClient([_t("T1", "Thing")], [{"ext_id": "P5", "name": "Supply",
                                                "drive_folder_id": "", "drive_folder_url": ""}])

    ensure_task_folder(client, _ident(), client.get_task("T1"), runner=drive)

    assert drive.created == [("PROJ", "Supply"), ("NEW1", "T1 Thing")]
    assert client.project_patches[0][1]["drive_folder_id"] == "NEW1"


def test_a_task_in_no_project_is_refused_rather_than_filed_flat():
    client = FakeClient([_t("T9", "Loose", project=None)])

    with pytest.raises(AgentGdocError, match="in no project"):
        ensure_task_folder(client, _ident(), client.get_task("T9"), runner=FakeDrive())


def test_two_folders_claiming_one_task_is_refused():
    drive = FakeDrive({"P5F": [{"id": "A", "name": "T71 one", "mimeType": FOLDER},
                               {"id": "B", "name": "T71 two", "mimeType": FOLDER}]})
    client = FakeClient([_t("T71", "x")], [P5])

    with pytest.raises(AgentGdocError, match="2 folders"):
        ensure_task_folder(client, _ident(), client.get_task("T71"), runner=drive)


def test_a_failed_write_back_does_not_fail_the_publish():
    drive, warned = FakeDrive({"P5F": []}), []
    client = FakeClient([_t("T71", "x")], [P5], fail_patch=True)

    fid = resolve_task_destination(_ident(), "T71", client=client, runner=drive, warn=warned.append)

    assert fid == "NEW1"
    assert "could not link it to T71" in warned[0]


def test_a_dry_run_writes_nothing():
    drive = FakeDrive({"P5F": []})
    client = FakeClient([_t("T71", "x")], [P5])

    got = ensure_task_folder(client, _ident(), client.get_task("T71"), runner=drive, dry_run=True)

    assert drive.created == [] and client.task_patches == []
    assert "would find-or-create" in got["folder_url"]


def test_gdoc_publish_task_refuses_project_beside_it(monkeypatch, tmp_path):
    md = tmp_path / "a.md"
    md.write_text("# hi\n")
    monkeypatch.setattr(agent_gdoc, "_gdoc_identity_from_opts", lambda *a: _ident())

    r = CliRunner().invoke(main, ["gdoc", "publish", "--md", str(md), "--name", "x",
                                  "--task", "T71", "--project", "P5", "--dry-run"])

    assert r.exit_code != 0
    assert "--task already names its project" in r.output


def test_gdoc_publish_task_resolves_through_the_task(monkeypatch, tmp_path):
    md = tmp_path / "a.md"
    md.write_text("# hi\n")
    seen = {}
    monkeypatch.setattr(agent_gdoc, "_gdoc_identity_from_opts", lambda *a: _ident())

    def fake_resolve(ident, ref, **kw):
        seen["ref"] = ref
        return "TASKFOLDER"

    monkeypatch.setattr("orchestrator.task_folder.resolve_task_destination", fake_resolve)

    def fake_publish(ident, *, parent, **kw):
        seen["parent"] = parent
        return {"id": "", "url": "", "dry_run": True}

    monkeypatch.setattr(agent_gdoc, "publish", fake_publish)

    r = CliRunner().invoke(main, ["gdoc", "publish", "--md", str(md), "--name", "x",
                                  "--task", "T71", "--dry-run"])

    assert r.exit_code == 0, r.output
    assert seen == {"ref": "T71", "parent": "TASKFOLDER"}
